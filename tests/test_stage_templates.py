"""Stage prompt templates stay well-formed prose (#382 regression)."""

from __future__ import annotations

import re

from orchestrator.schemas.enums import Stage
from orchestrator.stages import STAGE_SPECS

# A sentence end followed directly by the next sentence's capital, with no space between.
# That is how adjacent implicit-concatenated string literals read when one loses its trailing
# space, and no other test notices because the prompt still renders.
_JOINED_SENTENCES = re.compile(r"[a-z)][.!?][A-Z]")


def test_no_stage_template_joins_sentences_without_a_space() -> None:
    for stage, spec in STAGE_SPECS.items():
        template = spec.template or ""
        hits = [template[max(0, m.start() - 20): m.end() + 20]
                for m in _JOINED_SENTENCES.finditer(template)]
        assert not hits, f"{stage}: sentences joined without a space: {hits}"


def test_implement_template_opening_is_unchanged() -> None:
    # #382 recorded a null result and deliberately left IMPLEMENT's prompt text alone.
    assert STAGE_SPECS[Stage.IMPLEMENT].template.startswith(
        "Implement the change and commit it. Follow the scope plan in the context above"
    )
