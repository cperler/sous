"""Profile-driven scaffold (Phase 2): profile -> adapter + kit seeding, idempotent."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.ports.execution import (
    SUPPORTED,
    CapabilityDescriptor,
    ExecutionMode,
    Provider,
    Registry,
    default_registry,
)
from orchestrator.scaffold import (
    derive_worktree,
    detect_profile,
    load_kit_manifest,
    merge_profiles,
    profile_from_languages,
    read_profile,
    scaffold_adapter,
    select_kit_assets,
)
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

MANIFEST = load_kit_manifest()


def _origin_verifying_registry() -> Registry:
    """The default lanes plus a headless cell that preflights REVIEW's worktree origin.

    A scaffolded python adapter now declares the #391 worktree hooks, so the engine contains
    REVIEW on a lane that can honour them (``Engine._project_declares_worktree_origin``).
    The in-repo runners declare exactly that; ``default_registry`` has no headless cell.
    """
    reg = default_registry()
    reg.register_external(
        CapabilityDescriptor(
            execution_mode=ExecutionMode.HEADLESS,
            provider=Provider.CLAUDE,
            in_process=False,
            schema_enforced=True,
            verifies_worktree_origin=True,
            status=SUPPORTED,
        )
    )
    return reg


def _repo(tmp_path, files: dict[str, str], _ctr=[0]):  # noqa: B006 - unique dir per call
    _ctr[0] += 1
    root = tmp_path / f"repo{_ctr[0]}"
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def _import_adapter(dest, name: str):
    package = name.replace("-", "_")
    # Drop the whole package tree, not just its root: reloading `svc` alone re-executes
    # __init__.py but leaves a previously-cached `svc.config` in place, so a second adapter
    # scaffolded under the same name in another tmp_path would silently import the first
    # one's commands and probes.
    for cached in [m for m in sys.modules if m == package or m.startswith(f"{package}.")]:
        del sys.modules[cached]
    sys.path.insert(0, str(dest))
    try:
        return importlib.import_module(package)
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
    assert p.commands["test_unit"] == ["uv", "run", "python", "-m", "pytest", "-q"]
    assert p.commands["lint"] == ["uv", "run", "python", "-m", "ruff", "check", "."]
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
    assert cfg.agent_for(Stage.IMPLEMENT, "implement") == "python-backend-developer"
    assert cfg.test_unit_cmd() == ["uv", "run", "python", "-m", "pytest", "-q"]
    # #412: profile `lint` lands on the engine's LINT leg (typecheck_cmd) and profile
    # `typecheck` on its STATIC-TYPING leg (types_cmd) — the pair both merge gates call.
    assert cfg.typecheck_cmd() == ["uv", "run", "python", "-m", "ruff", "check", "."]
    assert cfg.types_cmd() == ["uv", "run", "python", "-m", "mypy", "."]
    assert not hasattr(cfg, "lint_cmd")  # exactly one method name per engine leg
    assert cfg.schema_for("test")["title"] == "test"  # schema_for inherits canonical
    # profile.toml round-trips
    rp = read_profile(tmp_path / "py_svc" / "profile.toml")
    assert rp.languages == ["python"] and rp.roster["implement"] == "python-backend-developer"
    assert rp.commands["lint"] == ["uv", "run", "python", "-m", "ruff", "check", "."]


def test_generated_review_gate_blocks_red_lint_and_overrides_approval(
    tmp_path, monkeypatch
) -> None:
    """A declared lint command must be live at REVIEW, not decorative profile data."""
    prof = profile_from_languages("gate-svc", ["python"], MANIFEST)
    scaffold_adapter("gate-svc", tmp_path, profile=prof)
    mod = _import_adapter(tmp_path, "gate-svc")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"T1": {"title": "lint me"}}))
    cfg = mod.get_config().__class__(tasks_path=str(tasks))
    outcomes = {
        "ruff": subprocess.CompletedProcess(["ruff"], 0, "", ""),
        "mypy": subprocess.CompletedProcess(["mypy"], 0, "", ""),
    }
    calls: list[tuple[list[str], str]] = []

    def fake_run(argv, cwd):  # noqa: ANN001 - mirrors the generated subprocess seam
        calls.append((argv, cwd))
        tool = next(tool for tool in outcomes if tool in argv)
        return outcomes[tool]

    monkeypatch.setattr(cfg, "_run_gate", fake_run)
    assert cfg.review_findings(worktree=str(tmp_path)) == []
    assert ["ruff" in argv for argv, _ in calls] == [True, False]
    assert all(cwd == str(tmp_path) for _, cwd in calls)

    noise = "x" * 2500 + "\nclassifier.py:4:101: E501 line too long"
    outcomes["ruff"] = subprocess.CompletedProcess(["ruff"], 1, noise, "")
    findings = cfg.review_findings(worktree=str(tmp_path))
    lint_finding = next(finding for finding in findings if "lint gate" in finding["description"])
    assert lint_finding["blocking"] is True
    assert lint_finding["severity"] == "critical"
    assert "uv run python -m ruff check ." in lint_finding["description"]
    assert "E501 line too long" in lint_finding["description"]
    assert len(lint_finding["description"]) < 2300  # noisy output is tail-capped

    eng = Engine(
        StatusStore(tmp_path / "run"),
        CostLedger(tmp_path / "costs.jsonl"),
        cfg,
        meta_task_source=cfg.task_source,
        registry=_origin_verifying_registry(),
    )
    eng.create_run("r1")
    eng.add_task("r1", "T1")
    while True:
        work = eng.next_work("r1", "T1")
        assert work is not None
        if work.stage is Stage.REVIEW:
            break
        output = None
        if work.stage is Stage.INTAKE:
            output = {"branch": "task/T1", "worktree": str(tmp_path),
                      "baseline_captured": True}
        eng.record("r1", make_result(work, structured_output=output))

    result = eng.record(
        "r1", make_result(work, structured_output={"approved": True, "issues": []})
    )
    assert result["outcome"] == "review_rejected_fix_cycle"
    assert "ruff" in eng.store.load_task("r1", "T1").learnings[-1]


def test_generated_review_gate_reports_unrunnable_lint_as_advisory(
    tmp_path, monkeypatch
) -> None:
    prof = profile_from_languages("unverified-svc", ["python"], MANIFEST)
    scaffold_adapter("unverified-svc", tmp_path, profile=prof)
    cfg = _import_adapter(tmp_path, "unverified-svc").get_config()

    def fake_run(argv, cwd):  # noqa: ANN001, ARG001 - mirrors the generated subprocess seam
        if "ruff" in argv:
            raise FileNotFoundError("ruff missing")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cfg, "_run_gate", fake_run)
    findings = cfg.review_findings(worktree=str(tmp_path))
    assert len(findings) == 1
    assert findings[0]["blocking"] is False
    assert findings[0]["severity"] == "important"
    assert "UNVERIFIED" in findings[0]["description"]


def test_generated_adapter_self_locates_repo_root(tmp_path) -> None:
    """#368: intake worktrees must come from the product repo, not driver CWD.

    The generated config exposes ``repo_root`` pointing at the adapter dir's parent —
    for the real ``<repo>/.orchestration`` layout, the product repo itself — so the
    deterministic INTAKE runner never falls back to the engine checkout it runs from."""
    prof = profile_from_languages("rr-svc", ["python"], MANIFEST)
    scaffold_adapter("rr-svc", tmp_path, profile=prof)
    mod = _import_adapter(tmp_path, "rr-svc")
    cfg = mod.get_config()
    assert cfg.repo_root == str((tmp_path / "rr_svc").resolve().parent)


def test_generated_adapter_has_env_gated_notify(tmp_path, capsys) -> None:
    """Generated adapters carry the alerting seam by default: a stderr line always, email
    only when the environment configures SMTP. With no SMTP env (the suite scrubs it —
    tests must never mail the operator), notify must be a safe no-op that never raises."""
    prof = profile_from_languages("nt-svc", ["python"], MANIFEST)
    scaffold_adapter("nt-svc", tmp_path, profile=prof)
    mod = _import_adapter(tmp_path, "nt-svc")
    cfg = mod.get_config()
    cfg.notify("task_completed", {"summary": "done", "task_id": "#1"})
    assert "[orchestrator:task_completed] done" in capsys.readouterr().err


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
    assert p.commands["test_unit"] == ["uv", "run", "python", "-m", "pytest", "-q"]  # manifest default
    assert p.roster["implement"] == "python-backend-developer"
    assert p.task_source == "local-file"


def test_detect_python_poetry(tmp_path) -> None:
    root = _repo(tmp_path, {"pyproject.toml": "[tool.poetry]\n", "poetry.lock": ""})
    p = detect_profile(root, "svc", MANIFEST)
    assert p.commands["test_unit"] == ["poetry", "run", "python", "-m", "pytest", "-q"]
    assert p.commands["install"] == ["poetry", "install"]
    assert p.commands["lint"] == ["poetry", "run", "python", "-m", "ruff", "check", "."]


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
    assert p.commands["test_unit"] == ["uv", "run", "python", "-m", "pytest", "-q"]  # python wins
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


# --- worktree provenance (#391) ----------------------------------------------

def test_python_scaffold_declares_worktree_origin_defenses(tmp_path) -> None:
    """A scaffolded python adapter must ship the defenses this repo wrote for itself.

    Without them a REVIEW checkout inherits a copied `.venv` whose console-script shebangs
    point at the worktree it was built in, so the stage can validate another worktree's
    source and approve on a false green (#391).
    """
    root = _repo(tmp_path, {
        "pyproject.toml": "[project]\nname='svc'\n", "uv.lock": "",
        "src/svc/__init__.py": "", "tests/test_x.py": "",
    })
    prof = detect_profile(root, "svc", MANIFEST)
    scaffold_adapter("svc", tmp_path / "adapters", profile=prof)
    cfg = _import_adapter(tmp_path / "adapters", "svc").get_config()

    assert cfg.fresh_install_paths() == [".venv"]
    probes = cfg.worktree_origin_probes()
    # The kit defaults are module invocation (#396), which has no shebang to inherit — so
    # the runner's INTERPRETER is what must be proven, not a `.venv/bin/<script>` launcher
    # this profile would never invoke.
    assert [(name, kind) for name, _, kind in probes] == [
        ("uv run python interpreter", "launcher"),
        ("svc module", "source"),  # the detected package
    ]
    # The probe argv must run through the runner THIS profile declares.
    assert all(argv[:4] == ["uv", "run", "python", "-c"] for _, argv, _ in probes)
    assert probes[0][1][4] == "import sys; print(sys.executable)"
    assert "import svc as _m" in probes[1][1][4]
    assert not any(".venv/bin" in arg for _, argv, _ in probes for arg in argv)


def test_generated_probes_are_accepted_and_catch_a_foreign_interpreter(tmp_path) -> None:
    """The generated declarations must satisfy the execution adapter's probe contract.

    Exercised against a real interpreter in a real worktree, because the whole value of the
    hooks is what they do to an actual environment, not the shape of the tuples.
    """
    from adapters.execution.worktree_origin import verify_worktree_origin

    prof = profile_from_languages("svc", ["python"], MANIFEST)
    prof.worktree["source_modules"] = ["svc"]
    # A project that keeps BARE console scripts still declares launcher probes, so this
    # exercises that path too rather than letting the #396 default retire its coverage.
    prof.worktree["launcher_probes"] = [".venv/bin/pytest"]
    scaffold_adapter("svc", tmp_path / "adapters", profile=prof)
    cfg = _import_adapter(tmp_path / "adapters", "svc").get_config()

    worktree = tmp_path / "wt"
    (worktree / "svc").mkdir(parents=True)
    (worktree / "svc" / "__init__.py").write_text("")
    subprocess.run(  # noqa: S603 - a venv is the environment under test
        [sys.executable, "-m", "venv", "--without-pip", str(worktree / ".venv")], check=True
    )
    interpreter = worktree / ".venv" / "bin" / "python"

    class _Probed:
        """The generated probes with `uv run python` swapped for this worktree's own venv."""

        def worktree_origin_probes(self) -> list[tuple[str, list[str], str]]:
            return [
                (name, [str(interpreter), *argv[3:]], kind)
                for name, argv, kind in cfg.worktree_origin_probes()
            ]

    # Nothing installed yet: a launcher that does not exist cannot be pointing at another
    # worktree, so absence falls back to this venv's interpreter rather than a false red.
    assert verify_worktree_origin(_Probed(), worktree).trusted

    # An installed launcher whose shebang names ANOTHER worktree is exactly #391.
    foreign = tmp_path / "other" / ".venv" / "bin" / "python"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("")
    (worktree / ".venv" / "bin" / "pytest").write_text(f"#!{foreign}\n")
    verdict = verify_worktree_origin(_Probed(), worktree)
    assert not verdict.trusted
    assert [n["probe"] for n in verdict.notices] == [".venv/bin/pytest shebang interpreter"]
    assert verdict.notices[0]["notice"] == "worktree_origin_mismatch"

    # The same launcher, honestly built in this worktree, verifies.
    (worktree / ".venv" / "bin" / "pytest").write_text(f"#!{interpreter}\n")
    assert verify_worktree_origin(_Probed(), worktree).trusted


def test_module_form_derives_an_interpreter_probe_and_bare_scripts_still_derive_launchers() -> None:
    """The probe follows the COMMAND FORM, so moving the defaults to `python -m` (#396)
    swaps the launcher probe for an interpreter probe instead of silently dropping it."""
    module_form = derive_worktree(["python"], {
        "test_unit": ["uv", "run", "python", "-m", "pytest", "-q"],
        "typecheck": ["uv", "run", "python", "-m", "mypy", "."],
    }, MANIFEST)
    assert module_form["interpreter_probe"] == ["uv", "run", "python"]
    assert "launcher_probes" not in module_form  # nobody invokes `.venv/bin/pytest` here

    bare = derive_worktree(["python"], {
        "test_unit": ["uv", "run", "pytest", "-q"],
        "typecheck": ["uv", "run", "mypy", "."],
    }, MANIFEST)
    assert bare["launcher_probes"] == [".venv/bin/pytest", ".venv/bin/mypy"]
    assert "interpreter_probe" not in bare  # a shebang is the thing that can go stale

    # A profile mid-migration declares one of each, and gets one of each.
    mixed = derive_worktree(["python"], {
        "test_unit": ["uv", "run", "python", "-m", "pytest", "-q"],
        "typecheck": ["uv", "run", "mypy", "."],
    }, MANIFEST)
    assert mixed["interpreter_probe"] == ["uv", "run", "python"]
    assert mixed["launcher_probes"] == [".venv/bin/mypy"]


def test_interpreter_probe_catches_a_runner_resolving_outside_the_worktree(tmp_path) -> None:
    """The #396 counterpart of the shebang case, against real interpreters.

    Module invocation cannot inherit a stale shebang, but the runner can still resolve to an
    environment belonging to another worktree (a stray VIRTUAL_ENV, a redirected project
    environment). That is what this probe has to make loud.
    """
    from adapters.execution.worktree_origin import verify_worktree_origin

    prof = profile_from_languages("svc", ["python"], MANIFEST)
    assert prof.worktree["interpreter_probe"] == ["uv", "run", "python"]
    scaffold_adapter("svc", tmp_path / "adapters", profile=prof)
    cfg = _import_adapter(tmp_path / "adapters", "svc").get_config()
    (name, argv, kind), = cfg.worktree_origin_probes()
    assert (name, kind) == ("uv run python interpreter", "launcher")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    for where in (worktree, tmp_path / "other"):
        subprocess.run(  # noqa: S603 - a venv is the environment under test
            [sys.executable, "-m", "venv", "--without-pip", str(where / ".venv")], check=True
        )

    def _probed(interpreter):
        """The generated probe with `uv run python` swapped for a concrete interpreter."""
        return type("P", (), {"worktree_origin_probes": lambda self: [
            (name, [str(interpreter), *argv[3:]], kind)
        ]})()

    # `.venv/bin/python` is itself a symlink to a SHARED base interpreter, so this also
    # pins that a healthy in-tree venv is trusted rather than read as an outside path.
    own = worktree / ".venv" / "bin" / "python"
    assert own.is_symlink() and not own.resolve().is_relative_to(worktree.resolve())
    assert verify_worktree_origin(_probed(own), worktree).trusted

    # The same command run through ANOTHER worktree's environment is exactly the hazard.
    verdict = verify_worktree_origin(_probed(tmp_path / "other" / ".venv" / "bin" / "python"), worktree)
    assert not verdict.trusted
    assert [n["notice"] for n in verdict.notices] == ["worktree_origin_mismatch"]
    assert verdict.notices[0]["probe"] == "uv run python interpreter"


def test_worktree_table_round_trips_and_survives_a_rerun(tmp_path) -> None:
    dest = tmp_path / "adapters"
    scaffold_adapter("svc", dest, profile=profile_from_languages("svc", ["python"], MANIFEST))
    rp = read_profile(dest / "svc" / "profile.toml")
    assert rp.worktree["fresh_install_paths"] == [".venv"]
    assert rp.worktree["interpreter_probe"] == ["uv", "run", "python"]

    # A hand-added module probe must survive re-running the scaffold for another language.
    rp.worktree["source_modules"] = ["svc"]
    merged = merge_profiles(rp, profile_from_languages("svc", ["go"], MANIFEST), MANIFEST)
    assert merged.worktree["source_modules"] == ["svc"]
    assert merged.worktree["interpreter_probe"] == ["uv", "run", "python"]

    # Clearing the key in profile.toml is the opt-out, and a re-run must not resurrect it.
    rp.worktree["interpreter_probe"] = []
    opted_out = merge_profiles(rp, profile_from_languages("svc", ["go"], MANIFEST), MANIFEST)
    assert opted_out.worktree["interpreter_probe"] == []


def test_non_python_stack_declares_no_worktree_hooks(tmp_path) -> None:
    """No defaults to derive means no generated hooks — not a guessed, wrong one."""
    prof = profile_from_languages("gosvc", ["go"], MANIFEST)
    assert prof.worktree == {}
    scaffold_adapter("gosvc", tmp_path / "adapters", profile=prof)
    cfg = _import_adapter(tmp_path / "adapters", "gosvc").get_config()
    assert not hasattr(cfg, "fresh_install_paths")
    assert not hasattr(cfg, "worktree_origin_probes")


def test_out_of_tree_venv_manager_gets_no_launcher_probe(tmp_path) -> None:
    """poetry keeps its env outside the tree, so a `.venv/bin/pytest` probe would be a
    false red on a healthy worktree — and so would an interpreter probe, since a healthy
    poetry interpreter legitimately lives outside it. That profile declares the module
    probe only."""
    root = _repo(tmp_path, {
        "pyproject.toml": "[tool.poetry]\n", "poetry.lock": "", "svc/__init__.py": "",
    })
    prof = detect_profile(root, "svc", MANIFEST)
    assert prof.worktree["fresh_install_paths"] == [".venv"]
    assert "launcher_probes" not in prof.worktree
    assert "interpreter_probe" not in prof.worktree
    assert prof.worktree["source_modules"] == ["svc"]
    assert prof.worktree["python"] == ["poetry", "run", "python"]


def test_gate_claimed_by_another_toolchain_is_skipped_not_mis_sliced(tmp_path) -> None:
    """A mixed stack shares the `test_unit`/`typecheck` keys, and the non-python toolchain
    can win them. Deriving the launcher by POSITION alone would slice `pnpm exec tsc` at the
    `uv run` offset and declare `.venv/bin/tsc` — a file that can never exist, so the probe
    would fall back to the interpreter and pass forever, silently reopening #391."""
    for prof in (
        profile_from_languages("svc", ["typescript", "python"], MANIFEST),
        merge_profiles(
            profile_from_languages("svc", ["typescript"], MANIFEST),
            profile_from_languages("svc", ["python"], MANIFEST),
            MANIFEST,
        ),
    ):
        # The python runner is still resolved (via `lint`), so the module probe survives.
        assert prof.worktree["python"] == ["uv", "run", "python"]
        assert prof.worktree["fresh_install_paths"] == [".venv"]
        # But no launcher or interpreter probe is invented for a gate python does not own.
        assert prof.worktree.get("launcher_probes", []) == []
        assert prof.worktree.get("interpreter_probe", []) == []

    # The single-language case still probes the runner it really uses.
    pure = profile_from_languages("svc", ["python"], MANIFEST)
    assert pure.worktree["interpreter_probe"] == ["uv", "run", "python"]


# --- the merge gates run the SAME legs the REVIEW gate does (#412) -------------------

def test_generated_adapter_feeds_both_merge_gate_legs(tmp_path, monkeypatch) -> None:
    """#412: a scaffolded adapter must put its linter on the engine's LINT leg and its type
    checker on the STATIC-TYPING leg.

    The old mapping (`typecheck` -> `typecheck_cmd`, no `types_cmd`) made both merge gates
    run mypy under the label "typecheck", never run ruff at all, and record `types` as
    absent — so a batch could pass its integration gate, merge, pass its trunk gate, and
    leave trunk red on the linter CI enforces.
    """
    prof = profile_from_languages("gate-legs", ["python"], MANIFEST)
    scaffold_adapter("gate-legs", tmp_path, profile=prof)
    cfg = _import_adapter(tmp_path, "gate-legs").get_config()
    eng = Engine(
        StatusStore(tmp_path / "run"), CostLedger(tmp_path / "costs.jsonl"), cfg,
        meta_task_source=cfg.task_source,
        registry=_origin_verifying_registry(),
    )

    def fake_run(argv, **kwargs):  # noqa: ANN001 - the engine's subprocess seam
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("orchestrator.engine.subprocess.run", fake_run)
    commands, skipped = eng._run_verification_commands(tmp_path, timeout_s=5)

    by_name = {c["name"]: c["argv"] for c in commands}
    assert by_name["typecheck"] == ["uv", "run", "python", "-m", "ruff", "check", "."]
    assert by_name["types"] == ["uv", "run", "python", "-m", "mypy", "."]
    # The static-typing leg is RUN, not recorded absent (the pre-#412 symptom).
    assert "types" not in [s["name"] for s in skipped]


def test_single_static_analysis_command_stays_on_the_primary_leg(tmp_path) -> None:
    """A stack declaring only `typecheck` (typescript: tsc) keeps it on `typecheck_cmd`,
    the leg every adapter must have, rather than moving its only gate onto the optional
    duck-typed one."""
    prof = profile_from_languages("ts-svc", ["typescript"], MANIFEST)
    assert "lint" not in prof.commands  # premise: the typescript stack declares no linter
    scaffold_adapter("ts-svc", tmp_path, profile=prof)
    cfg = _import_adapter(tmp_path, "ts-svc").get_config()

    assert cfg.typecheck_cmd() == ["pnpm", "exec", "tsc", "--noEmit"]
    assert cfg.types_cmd() == ["true"]  # no-op sentinel; the gate records it as skipped
