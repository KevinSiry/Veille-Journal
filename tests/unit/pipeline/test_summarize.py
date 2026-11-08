import pytest

from veille.contracts import Item, LLMError, LLMUnavailableError, Origin
from veille.llm import FakeLLM
from veille.pipeline.dedupe import Cluster
from veille.pipeline.prompts import (
    SUMMARY_FEW_SHOTS,
    SUMMARY_MAX_HEADLINE_WORDS,
    SUMMARY_MAX_WORDS,
    SUMMARY_SCHEMA,
    SUMMARY_SYSTEM,
    build_summary_user,
)
from veille.pipeline.summarize import (
    clean_headline,
    clean_summary,
    first_sentence,
    strip_boilerplate,
    summarize_cluster,
)

from .fakes import NOW, make_theme

OK = {"relevant": True, "headline_fr": "Titre", "summary_fr": "Une phrase."}


def cluster(*titles: str) -> Cluster:
    items = tuple(
        Item(
            f"https://x/{i}",
            f"https://x/{i}",
            t,
            NOW,
            NOW,
            "",
            f"S{i}",
            Origin.RSS,
            f"Contenu {i}.",
            "h",
            relevance=1.0 - i / 10,
        )
        for i, t in enumerate(titles)
    )
    return Cluster(items)


def test_success_and_prompt_content() -> None:
    llm = FakeLLM([OK])
    out = summarize_cluster(cluster("Main title", "Other title"), make_theme(), llm)
    assert out.entry is not None and out.llm_calls == 1 and out.tokens > 0
    assert out.entry.headline_fr == "Titre" and out.entry.summary_fr == "Une phrase."
    assert [link.name for link in out.entry.source_links] == ["S0", "S1"]
    system, user, schema = llm.calls[0]
    assert system == SUMMARY_SYSTEM and schema == SUMMARY_SCHEMA
    assert "Titre : Main title" in user and "Other title" in user and "Contenu 0." in user
    assert "Azure Cloud" in user


def test_invalid_json_retry_then_drop() -> None:
    llm = FakeLLM(["{not json", "still bad"])
    out = summarize_cluster(cluster("t"), make_theme(), llm)
    assert out.entry is None and out.llm_calls == 2 and "llm-error" in out.reason


def test_retry_success_and_schema_violation() -> None:
    llm = FakeLLM([{"relevant": "yes", "headline_fr": "x", "summary_fr": "y"}, OK])
    out = summarize_cluster(cluster("t"), make_theme(), llm)
    assert out.entry is not None and out.llm_calls == 2
    llm = FakeLLM([{"relevant": True, "headline_fr": "x", "summary_fr": 3}, OK])
    assert summarize_cluster(cluster("t"), make_theme(), llm).entry is not None


def test_not_relevant_and_empty() -> None:
    llm = FakeLLM([{**OK, "relevant": False}])
    out = summarize_cluster(cluster("t"), make_theme(), llm)
    assert out.entry is None and out.reason == "not-relevant"
    llm = FakeLLM([{**OK, "headline_fr": "  "}])
    assert summarize_cluster(cluster("t"), make_theme(), llm).reason == "empty"


def test_unavailable_twice_raises_once_retries() -> None:
    llm = FakeLLM([LLMUnavailableError("down"), LLMUnavailableError("down")])
    with pytest.raises(LLMUnavailableError):
        summarize_cluster(cluster("t"), make_theme(), llm)
    llm = FakeLLM([LLMUnavailableError("down"), OK])
    assert summarize_cluster(cluster("t"), make_theme(), llm).entry is not None
    llm = FakeLLM([LLMUnavailableError("down"), LLMError("bad")])
    assert summarize_cluster(cluster("t"), make_theme(), llm).entry is None


def test_tokens_from_last_usage() -> None:
    llm = FakeLLM([OK])
    llm.last_usage = 42
    assert summarize_cluster(cluster("t"), make_theme(), llm).tokens == 42


def test_post_validation() -> None:
    assert strip_boilerplate("Cet article explique que Microsoft a lancé X.") == (
        "Microsoft a lancé X."
    )
    assert strip_boilerplate("L'article indique qu'une panne a eu lieu.") == (
        "Une panne a eu lieu."
    )
    assert strip_boilerplate("Selon l'article, le prix baisse.") == "Le prix baisse."
    assert strip_boilerplate("Microsoft a lancé X.") == "Microsoft a lancé X."
    assert first_sentence("M. Dupont a parlé. Puis autre chose.") == "M. Dupont a parlé."
    assert first_sentence("J. Doe signe. Ensuite.") == "J. Doe signe."
    assert first_sentence("Sans point final") == "Sans point final"
    assert clean_summary("Une phrase sans point") == "Une phrase sans point."
    assert clean_summary("Phrase un ! Phrase deux.") == "Phrase un !"
    long_text = " ".join(f"mot{i}" for i in range(50)) + "."
    trimmed = clean_summary(long_text)
    assert trimmed.endswith("…") and len(trimmed.split()) == SUMMARY_MAX_WORDS
    long_headline = " ".join(f"m{i}" for i in range(20)) + "."
    assert len(clean_headline(long_headline).split()) == SUMMARY_MAX_HEADLINE_WORDS
    assert clean_headline("« Un titre. »") == "Un titre"


def test_few_shots_respect_rules() -> None:
    for user, answer in SUMMARY_FEW_SHOTS:
        assert set(answer) == set(SUMMARY_SCHEMA["required"])
        assert len(answer["headline_fr"].split()) <= SUMMARY_MAX_HEADLINE_WORDS
        assert len(answer["summary_fr"].split()) <= SUMMARY_MAX_WORDS
        assert clean_summary(answer["summary_fr"]) == answer["summary_fr"]
        assert user in SUMMARY_SYSTEM


def test_build_summary_user_truncates_and_defaults() -> None:
    user = build_summary_user(
        theme_name="T",
        theme_description="",
        title="t",
        source_name="",
        content="z" * 4000,
        other_titles=["a", " ", "b", "c", "d"],
    )
    assert "(aucune)" in user and "(inconnue)" in user
    assert "z" * 3000 in user and "z" * 3001 not in user
    assert "a | b | c" in user and "d" not in user.split("Texte :")[0].split("Autres")[1]
    empty = build_summary_user(
        theme_name="T", theme_description="d", title="t", source_name="s", content=" "
    )
    assert "texte indisponible" in empty
