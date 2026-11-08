import re
from datetime import date

from markdown_it import MarkdownIt

from veille.contracts import EMPTY_ARTICLE_TEXT, ArticleStatus, Entry, SourceLink
from veille.pipeline.render import (
    article_title,
    escape_md,
    french_long_date,
    render_article,
    render_body,
    safe_url,
)

from .fakes import NOW, make_theme

DAY = date(2026, 11, 8)


def test_french_dates() -> None:
    assert french_long_date(DAY) == "dimanche 8 novembre 2026"
    assert french_long_date(date(2026, 2, 1)) == "dimanche 1er février 2026"
    assert french_long_date(date(2026, 8, 17)) == "lundi 17 août 2026"
    assert french_long_date(date(2026, 12, 31)) == "jeudi 31 décembre 2026"
    assert article_title("Azure Cloud", DAY) == "Azure Cloud — dimanche 8 novembre 2026"


def test_render_body_format() -> None:
    entry = Entry(
        "Headline",
        "Summary.",
        (
            SourceLink("Source A", "https://a/x", "https://a/x"),
            SourceLink("Source B", "https://b/y", "https://b/y"),
        ),
    )
    body = render_body(article_title("Azure Cloud", DAY), [entry])
    assert body == (
        "## Azure Cloud — dimanche 8 novembre 2026\n\n"
        "- **Headline** — Summary. ([Source A](https://a/x) · [Source B](https://b/y))\n"
    )


def test_render_empty() -> None:
    draft = render_article(make_theme(), DAY, [], model="m", generated_at=NOW)
    assert draft.status is ArticleStatus.EMPTY
    assert draft.body_md == f"## Azure Cloud — dimanche 8 novembre 2026\n\n{EMPTY_ARTICLE_TEXT}\n"
    assert draft.title == "Azure Cloud — dimanche 8 novembre 2026"


def test_escaping_prevents_injection() -> None:
    entry = Entry(
        "[x](javascript:alert(1)) *bold* <script>",
        "Click [here](https://evil) `code` _it_ <img src=x onerror=alert(1)>",
        (
            SourceLink("Evil](javascript:alert(1))[", "https://ok/a b(c)", "u1"),
            SourceLink("JS", "javascript:alert(1)", "u2"),
            SourceLink("Ctrl", "https://ok/\nx", "u3"),
            SourceLink("Bad", "http://[::1", "u4"),
        ),
    )
    draft = render_article(make_theme(), DAY, [entry], model="m", generated_at=NOW)
    assert draft.status is ArticleStatus.OK and draft.entry_count == 1
    html = MarkdownIt("commonmark").render(draft.body_md)
    assert re.findall(r'href="([^"]*)"', html) == ["https://ok/a%20b%28c%29"]
    assert html.count("<a ") == 1
    assert 'href="https://ok/a%20b%28c%29"' in html
    assert "<script" not in html and "<img" not in html and "<strong>bold" not in html
    assert "<code>" not in html and "<em>" not in html
    assert "[x](javascript:alert(1))" in html  # rendered as plain text


def test_escape_helpers() -> None:
    assert escape_md("a\nb  *c*") == r"a b \*c\*"
    assert safe_url("ftp://x") is None
    assert safe_url("https://x/a)b") == "https://x/a%29b"
