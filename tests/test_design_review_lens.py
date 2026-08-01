"""#62: the design-review lens — a deterministic, project-agnostic criteria block the
REVIEW prompt grows when the change touches frontend files (files_changed folded from
IMPLEMENT). Sibling of the #41 docs-only directive: engine-template-side, no model trust
needed (it only ADDS scrutiny). Project-specific design tokens stay in the adapter agent.
"""

from __future__ import annotations

from orchestrator.schemas.enums import Stage
from orchestrator.stages import _has_frontend_change, render_prompt

_LENS_MARKER = "apply the design-review lens"


def _review_prompt(files_changed) -> str:
    return render_prompt(
        Stage.REVIEW,
        task_id="t1",
        title="tweak the dashboard",
        body="",
        context={"pr_url": "http://x/pr/1", "files_changed": files_changed},
    )


# --- the frontend-file signal -----------------------------------------------------------
def test_has_frontend_change_recognizes_ui_files_and_paths() -> None:
    assert _has_frontend_change(["src/components/Card.tsx"])
    assert _has_frontend_change(["app/styles/theme.css"])
    assert _has_frontend_change(["ui/Widget.vue"])
    assert _has_frontend_change(["web/Page.svelte"])
    assert _has_frontend_change(["frontend/src/lib/util.ts"])  # under a frontend/ segment
    assert _has_frontend_change(["backend/api.py", "frontend/App.jsx"])  # any hit counts


def test_has_frontend_change_rejects_backend_and_bad_input() -> None:
    assert not _has_frontend_change(["lambda/handler.py", "docs/README.md"])
    assert not _has_frontend_change([])
    assert not _has_frontend_change(None)
    assert not _has_frontend_change("frontend/App.tsx")  # a str, not a list -> no fold


# --- the REVIEW prompt conditional ------------------------------------------------------
def test_review_prompt_includes_lens_for_frontend_change() -> None:
    prompt = _review_prompt(["frontend/src/Login.tsx", "frontend/src/login.css"])
    assert _LENS_MARKER in prompt
    assert "Visual hierarchy" in prompt and "Accessibility" in prompt


def test_review_prompt_omits_lens_for_backend_change() -> None:
    prompt = _review_prompt(["lambda/suggest/handler.py"])
    assert _LENS_MARKER not in prompt


def test_lens_is_review_only_not_implement() -> None:
    # The lens extends the REVIEW guidance only; the IMPLEMENT prompt is untouched even
    # when files_changed is present in context.
    impl = render_prompt(
        Stage.IMPLEMENT,
        task_id="t1",
        title="x",
        body="",
        context={"files_changed": ["frontend/App.tsx"]},
    )
    assert _LENS_MARKER not in impl


def test_lens_absent_when_no_files_changed_context() -> None:
    assert _LENS_MARKER not in _review_prompt(None)
    prompt = render_prompt(Stage.REVIEW, task_id="t1", title="x", body="", context=None)
    assert _LENS_MARKER not in prompt
