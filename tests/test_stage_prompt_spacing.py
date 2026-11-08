"""Stage templates are built from joined string literals, so a piece that ends without its
trailing space runs two sentences together. This catches that shape in every template. It
does not catch two words run together, which has no pattern that separates it from prose."""

import re

import pytest

from orchestrator.stages import STAGE_SPECS

_SENTENCES_RUN_TOGETHER = re.compile(r"[a-z)`]\.[A-Z]")


def _run_together(template: str) -> list[str]:
    return [
        template[max(0, m.start() - 30) : m.end() + 20]
        for m in _SENTENCES_RUN_TOGETHER.finditer(template)
    ]


@pytest.mark.parametrize("stage", list(STAGE_SPECS), ids=lambda s: s.value)
def test_stage_template_has_no_sentences_run_together(stage) -> None:
    assert _run_together(STAGE_SPECS[stage].template or "") == []


def test_the_check_catches_a_dropped_space() -> None:
    assert _run_together("Open the file." "Then stop.") == ["Open the file.Then stop."]
    assert _run_together("Open the file. Then stop. See orchestrator.stages.") == []
