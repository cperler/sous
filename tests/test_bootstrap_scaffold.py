"""Profile-driven scaffold (Phase 2): profile -> adapter + kit seeding, idempotent."""

from __future__ import annotations

import importlib
import sys

from orchestrator.scaffold import (
    detect_profile,
    load_kit_manifest,
    merge_profiles,
    profile_from_languages,
    read_profile,
    scaffold_adapter,
    select_kit_assets,
)

MANIFEST = load_kit_manifest()


def _repo(tmp_path, files: dict[str, str], _ctr=[0]):  # noqa: B006 - unique dir per call
    _ctr[0] += 1
    root = tmp_path / f"repo{_ctr[0]}"
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def _import_adapter(dest, name: str):
    sys.path.insert(0, str(dest))
    try:
        mod = importlib.import_module(name.replace("-", "_"))
        importlib.reload(mod)  # avoid cross-test caching of a same-named package
        return mod
    finally:
        sys.path.remove(str(dest))


# --- profile synthesis -------------------------------------------------------

def test_profile_selects_stack_agents_and_commands() -> None:
    p = profile_from_languages("svc", ["python", "typescript"], MANIFEST)
    # implement goes to the python stack agent (wins over generic-implementer); the
    # frontend sub-role to the ts agent; the test stage stays the generic validator.
    assert p.roster["implement"] == "python-backend-developer"
    assert p.roster["implement:frontend"] == "typescript-frontend-developer"
    assert p.roster["test"] == "test-validator"
    assert p.roster["review"] == "code-reviewer"
    # commands unioned from both stacks' manifest defaults
    assert p.commands["test_unit"] == ["uv", "run", "pytest", "-q"]
    assert p.commands["test_e2e"] == ["pnpm", "exec", "playwright", "test"]
    # seed picks generic + both stacks' agents/hooks
    assert "python-backend-developer" in p.seed["agents"]
    assert "typescript-frontend-developer" in p.seed["agents"]
    assert set(p.seed["hooks"]) == {"python-format", "typescript-format",
                                "guard-deploy", "guard-sensitive-files"}  # guards always ride


def test_design_reviewer_seeds_with_the_frontend_stack_only() -> None:
    # #62: the generic design-review lens agent rides the frontend (typescript) stack, and
    # claims the review:design sub-role — a pure-python project doesn't get it.
    ts = profile_from_languages("svc", ["typescript"], MANIFEST)
    assert "design-reviewer" in ts.seed["agents"]
    assert ts.roster["review:design"] == "design-reviewer"
    py = profile_from_languages("svc", ["python"], MANIFEST)
    assert "design-reviewer" not in py.seed["agents"]
    assert "review:design" not in py.roster


def test_no_language_profile_is_generic() -> None:
    p = profile_from_languages("svc", [], MANIFEST)
    assert p.roster["implement"] == "generic-implementer"
    assert p.seed["hooks"] == ["guard-deploy", "guard-sensitive-files"]  # safety is stack-agnostic
    assert p.commands == {}


# --- generated adapter reflects the profile ----------------------------------

def test_generated_adapter_reflects_profile(tmp_path) -> None:
    prof = profile_from_languages("py-svc", ["python"], MANIFEST)
    scaffold_adapter("py-svc", tmp_path, profile=prof)
    mod = _import_adapter(tmp_path, "py-svc")
    cfg = mod.get_config()
    from orchestrator.schemas.enums import Stage
    assert cfg.agent_for(Stage.IMPLEMENT, "implement") == "python-backend-developer"
    assert cfg.test_unit_cmd() == ["uv", "run", "pytest", "-q"]
    assert cfg.schema_for("test")["title"] == "test"  # schema_for inherits canonical
    # profile.toml round-trips
    rp = read_profile(tmp_path / "py_svc" / "profile.toml")
    assert rp.languages == ["python"] and rp.roster["implement"] == "python-backend-developer"


# --- kit seeding into a project root -----------------------------------------

def test_seeds_kit_into_project_root(tmp_path) -> None:
    proj = tmp_path / "proj"
    prof = profile_from_languages("svc", ["python"], MANIFEST)
    scaffold_adapter("svc", tmp_path / "adapters", profile=prof, into=proj)
    claude = proj / ".claude"
    assert (claude / "agents" / "python-backend-developer.md").exists()
    assert (claude / "agents" / "code-reviewer.md").exists()       # generic too
    # Skills seed as .claude/skills/<name>/SKILL.md (invocable slash commands), keyed by
    # the frontmatter name — NOT a flat, undiscovered .md file.
    skill = claude / "skills" / "orchestrate-task-interactive" / "SKILL.md"
    assert skill.exists()
    assert "name: orchestrate-task-interactive" in skill.read_text()
    assert (claude / "skills" / "orchestrate-batch-interactive" / "SKILL.md").exists()
    assert not (claude / "skills" / "supervisor_skill.md").exists()  # no flat mirror
    assert (claude / "hooks" / "python-format.json").exists()
    assert not (claude / "agents" / "typescript-frontend-developer.md").exists()  # not in stack


# --- idempotent / additive re-run --------------------------------------------

def test_rerun_adds_language_without_clobbering_handedits(tmp_path) -> None:
    dest, proj = tmp_path / "adapters", tmp_path / "proj"
    scaffold_adapter("svc", dest, profile=profile_from_languages("svc", ["python"], MANIFEST), into=proj)
    # hand-edit the (write-once) classifier + a profile.toml command override
    classifier = dest / "svc" / "classifier.py"
    classifier.write_text(classifier.read_text() + "\n# HAND-EDITED\n")

    # re-run adding typescript
    scaffold_adapter("svc", dest, profile=profile_from_languages("svc", ["typescript"], MANIFEST), into=proj)

    # languages unioned; both stack agents now present; hand-edit preserved
    rp = read_profile(dest / "svc" / "profile.toml")
    assert set(rp.languages) == {"python", "typescript"}
    assert rp.roster["implement"] == "python-backend-developer"          # kept
    assert rp.roster["implement:frontend"] == "typescript-frontend-developer"  # added
    assert "# HAND-EDITED" in classifier.read_text()                     # not clobbered
    assert (proj / ".claude" / "agents" / "typescript-frontend-developer.md").exists()  # newly seeded
    assert (proj / ".claude" / "agents" / "design-reviewer.md").exists()  # #62: design lens rides ts


# --- stack detection (Phase 3 Part A) ----------------------------------------

def test_detect_python_uv(tmp_path) -> None:
    root = _repo(tmp_path, {"pyproject.toml": "[project]\nname='x'\n", "uv.lock": ""})
    p = detect_profile(root, "svc", MANIFEST)
    assert p.languages == ["python"]
    assert p.commands["test_unit"] == ["uv", "run", "pytest", "-q"]  # manifest default (uv)
    assert p.roster["implement"] == "python-backend-developer"
    assert p.task_source == "local-file"


def test_detect_python_poetry(tmp_path) -> None:
    root = _repo(tmp_path, {"pyproject.toml": "[tool.poetry]\n", "poetry.lock": ""})
    p = detect_profile(root, "svc", MANIFEST)
    assert p.commands["test_unit"] == ["poetry", "run", "pytest", "-q"]
    assert p.commands["install"] == ["poetry", "install"]


def test_detect_typescript_pnpm_with_playwright(tmp_path) -> None:
    root = _repo(tmp_path, {
        "tsconfig.json": "{}", "pnpm-lock.yaml": "", "playwright.config.ts": "export default {}",
    })
    p = detect_profile(root, "ui", MANIFEST)
    assert p.languages == ["typescript"]
    assert p.commands["test_unit"] == ["pnpm", "test"]
    assert p.commands["test_e2e"] == ["pnpm", "exec", "playwright", "test"]  # playwright present


def test_detect_drops_e2e_without_playwright(tmp_path) -> None:
    root = _repo(tmp_path, {"tsconfig.json": "{}", "pnpm-lock.yaml": ""})
    p = detect_profile(root, "ui", MANIFEST)
    assert "test_e2e" not in p.commands  # no playwright -> no e2e command


def test_detect_node_vs_typescript(tmp_path) -> None:
    root = _repo(tmp_path, {"package.json": '{"name":"x"}', "package-lock.json": ""})
    p = detect_profile(root, "svc", MANIFEST)
    assert p.languages == ["node"]  # package.json without typescript
    assert p.commands["install"] == ["npm", "ci"]


def test_detect_mixed_python_first_wins_shared_keys(tmp_path) -> None:
    root = _repo(tmp_path, {
        "pyproject.toml": "[project]\n", "uv.lock": "",
        "tsconfig.json": "{}", "pnpm-lock.yaml": "", "playwright.config.ts": "",
    })
    p = detect_profile(root, "svc", MANIFEST)
    assert set(p.languages) == {"python", "typescript"}
    assert p.commands["test_unit"] == ["uv", "run", "pytest", "-q"]   # python (first) wins
    assert p.commands["test_e2e"] == ["pnpm", "exec", "playwright", "test"]  # ts contributes e2e


def test_detect_go_and_rust(tmp_path) -> None:
    assert detect_profile(_repo(tmp_path, {"go.mod": "module x"}), "g", MANIFEST).languages == ["go"]
    assert detect_profile(_repo(tmp_path, {"Cargo.toml": "[package]"}), "r", MANIFEST).languages == ["rust"]


def test_detect_github_task_source(tmp_path) -> None:
    root = _repo(tmp_path, {
        "pyproject.toml": "[project]\n",
        ".git/config": '[remote "origin"]\n\turl = git@github.com:me/repo.git\n',
    })
    assert detect_profile(root, "svc", MANIFEST).task_source == "github-issues"


def test_merge_is_additive_and_incoming_wins() -> None:
    existing = profile_from_languages("svc", ["python"], MANIFEST)
    existing.commands["test_unit"] = ["custom", "test"]  # a hand override in profile.toml
    incoming = profile_from_languages("svc", ["go"], MANIFEST)
    merged = merge_profiles(existing, incoming, MANIFEST)
    assert set(merged.languages) == {"python", "go"}
    assert merged.commands["test_unit"] == ["custom", "test"]     # existing override preserved
    assert merged.commands["install"]  # go/python install defaults present
    assert "generic-implementer" in select_kit_assets(merged, MANIFEST)["agents"]
