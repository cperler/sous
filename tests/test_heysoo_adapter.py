"""Hey Soo! reference adapter: classifier taxonomy + GitHub task source (mocked gh)."""

from __future__ import annotations

import json

from adapters.project.base import ProjectConfig, TaskSource
from adapters.project.heysoo import get_config
from adapters.project.heysoo.classifier import HeysooClassifier
from adapters.project.heysoo.task_source import GitHubIssuesSource
from orchestrator.schemas.enums import FailureKind, Stage


def test_config_satisfies_protocol() -> None:
    cfg = get_config()
    assert isinstance(cfg, ProjectConfig)
    assert isinstance(cfg.task_source, TaskSource)


def test_classifier_pytest_and_taxonomy() -> None:
    c = HeysooClassifier()
    out = c.classify("FAILED lambda/suggest/test_handler.py::test_x\nFAILED test_y.py::test_z\n")
    tests = {f.test for f in out}
    assert "lambda/suggest/test_handler.py::test_x" in tests
    assert all(f.kind is FailureKind.UNIT for f in out)


def test_classifier_playwright_e2e() -> None:
    c = HeysooClassifier()
    txt = "  ✘  3 [chromium] › tests/e2e/login.spec.ts:12:3 › user can log in\n"
    out = c.classify(txt)
    assert out[0].test == "tests/e2e/login.spec.ts:12:3"
    assert out[0].kind is FailureKind.E2E


def test_classifier_infra_short_circuits() -> None:
    c = HeysooClassifier()
    out = c.classify("Error: connect ECONNREFUSED 127.0.0.1:5173\nFAILED test_a.py::x\n")
    assert len(out) == 1 and out[0].kind is FailureKind.INFRA


def test_impacted_tests_mapping() -> None:
    c = HeysooClassifier()
    impacted = c.impacted_tests(
        ["lambda/suggest/handler.py", "frontend/src/Login.tsx", "tests/e2e/x.spec.ts"]
    )
    assert "lambda/suggest/test_handler.py" in impacted
    assert "frontend/src/Login.spec.ts" in impacted
    assert "tests/e2e/x.spec.ts" in impacted


def test_docstring_agent_is_generic_not_phpdoc() -> None:
    cfg = get_config()
    agent = cfg.agent_for(Stage.DELIVER, role="docstring")
    assert agent == "docstring-writer"
    assert agent != "phpdoc-writer"  # fix D13


def test_github_task_source_resolve_with_fake_gh() -> None:
    captured: list[list[str]] = []

    def fake_gh(argv: list[str]) -> str:
        captured.append(argv)
        return json.dumps(
            {"number": 505, "title": "Fix the thing", "body": "details", "labels": [{"name": "bug"}]}
        )

    src = GitHubIssuesSource("cperler/heysoo", runner=fake_gh)
    spec = src.resolve("#505")
    assert spec.issue_number == 505 and spec.title == "Fix the thing" and spec.labels == ["bug"]
    assert captured[0][:4] == ["gh", "issue", "view", "505"]


def test_github_mark_complete_posts_comment() -> None:
    seen: list[list[str]] = []
    src = GitHubIssuesSource("cperler/heysoo", runner=lambda a: seen.append(a) or "")
    src.mark_complete("#505", "https://github.com/x/y/pull/9")
    assert seen[0][:3] == ["gh", "issue", "comment"]
    assert "https://github.com/x/y/pull/9" in seen[0][-1]
