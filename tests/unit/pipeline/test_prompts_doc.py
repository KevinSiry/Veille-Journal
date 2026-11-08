"""docs/PROMPTS.md must mirror veille.pipeline.prompts verbatim."""

import json
from pathlib import Path

from veille.pipeline import prompts

DOC = Path(__file__).resolve().parents[3] / "docs" / "PROMPTS.md"


def test_prompts_doc_mirrors_source() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert f"`PROMPT_VERSION`: `{prompts.PROMPT_VERSION}`" in text
    assert prompts.SUMMARY_RULES in text
    assert prompts.SUMMARY_SYSTEM[len(prompts.SUMMARY_RULES) + 2 :] in text
    assert prompts.KEYWORDS_SYSTEM in text
    assert prompts.build_keywords_user("<nom>", "<description>") in text
    for schema in (prompts.SUMMARY_SCHEMA, prompts.KEYWORDS_SCHEMA):
        assert json.dumps(schema, ensure_ascii=False, indent=2) in text
