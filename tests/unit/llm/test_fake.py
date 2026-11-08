import pytest

from veille.contracts import LLM, LLMError
from veille.llm import FakeLLM
from veille.pipeline.prompts import (
    KEYWORDS_SCHEMA,
    KEYWORDS_SYSTEM,
    SUMMARY_SCHEMA,
    SUMMARY_SYSTEM,
    build_keywords_user,
    build_summary_user,
)
from veille.pipeline.text import cosine


def test_implements_protocol() -> None:
    assert isinstance(FakeLLM(), LLM)


def test_embeddings_similarity() -> None:
    llm = FakeLLM()
    a, b, c = llm.embed(
        [
            "Microsoft annonce une panne Azure en Europe",
            "Microsoft annonce une panne d'Azure en Europe !",
            "Recette de la tarte aux pommes",
        ]
    )
    assert cosine(a, b) > 0.88
    assert cosine(a, c) < 0.2
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9
    assert llm.embed(["Élection"]) == llm.embed(["election"])
    assert llm.embed([""])[0] == [0.0] * llm.dim


def test_default_summary_and_keywords() -> None:
    llm = FakeLLM()
    user = build_summary_user(
        theme_name="T",
        theme_description="d",
        title="Azure ajoute des GPU dans deux régions européennes aujourd'hui même",
        source_name="S",
        content="Première phrase ici. Deuxième phrase.",
    )
    out = llm.chat_json(SUMMARY_SYSTEM, user, SUMMARY_SCHEMA)
    assert out == {
        "relevant": True,
        "headline_fr": "Azure ajoute des GPU dans deux régions européennes",
        "summary_fr": "Première phrase ici.",
    }
    kw = llm.chat_json(KEYWORDS_SYSTEM, build_keywords_user("Azure Cloud", ""), KEYWORDS_SCHEMA)
    assert kw["keywords"][:2] == ["azure", "cloud"] and len(kw["keywords"]) >= 5
    assert len(llm.calls) == 2


def test_scripted_responses() -> None:
    llm = FakeLLM(["{bad", "[1]", '{"a": 1}', {"b": 2}, LLMError("x")])
    for _ in range(2):
        with pytest.raises(LLMError):
            llm.chat_json("s", "u", {})
    assert llm.chat_json("s", "u", {}) == {"a": 1}
    assert llm.chat_json("s", "u", {}) == {"b": 2}
    with pytest.raises(LLMError):
        llm.chat_json("s", "u", {})
    assert llm.chat_json("s", "Titre : Hello\nTexte :\n", {})["summary_fr"] == "Hello."


def test_embed_error() -> None:
    with pytest.raises(LLMError):
        FakeLLM(embed_error=LLMError("down")).embed(["a"])
