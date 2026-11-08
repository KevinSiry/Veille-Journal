from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from veille.contracts import HIGHLIGHT_END, HIGHLIGHT_START
from veille.web.rendering import fr_date, highlight_snippet, render_markdown

from .conftest import Env


def test_markdown_links_get_rel_and_target() -> None:
    html = render_markdown("Voir [Le Monde](https://www.lemonde.fr/a?b=1&c=2).")
    assert (
        '<a href="https://www.lemonde.fr/a?b=1&amp;c=2" '
        'rel="noopener noreferrer nofollow" target="_blank">Le Monde</a>'
    ) in html


def test_markdown_blocks_dangerous_links_and_html() -> None:
    html = render_markdown(
        "[x](javascript:alert(1)) [y](JaVaScRiPt:alert(1)) [d](data:text/html,hi) "
        "[m](mailto:a@b.c) [r](/relative) <script>alert(1)</script> <a href='https://e'>e</a>"
    )
    assert "<a" not in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_markdown_autolink_and_images() -> None:
    html = render_markdown("<https://ex.fr> ![img](https://ex.fr/i.png) <javascript:alert(1)>")
    assert html.count("<a ") == 2  # image syntax degrades to a plain link
    assert 'target="_blank"' in html
    assert "<img" not in html


def test_markdown_in_page_is_escaped(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("T", "", [])
    env.store.add_article(
        theme,
        date(2026, 11, 8),
        title="<script>alert(1)</script>",
        body_md="Fait <script>alert(2)</script> [src](https://ex.fr) [bad](javascript:alert(3))",
    )
    for path in ("/", "/archive/2026-11-08"):
        html = auth_client.get(path).text
        assert "<script>alert" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert 'href="https://ex.fr" rel="noopener noreferrer nofollow" target="_blank"' in html
        assert 'href="javascript' not in html


def test_highlight_escapes_first() -> None:
    snippet = f"<b>{HIGHLIGHT_START}x&y{HIGHLIGHT_END}</b>"
    assert str(highlight_snippet(snippet)) == "&lt;b&gt;<mark>x&amp;y</mark>&lt;/b&gt;"


def test_fr_date() -> None:
    assert fr_date(date(2026, 8, 15)) == "samedi 15 août 2026"


def test_generated_entries_get_presentation_hooks() -> None:
    from veille.web.rendering import render_markdown

    html = str(
        render_markdown(
            "## T\n\n- **Titre <b>** — Résumé (x) ([Le Monde](https://a.fr/1) · [AFP](https://b.fr))\n"
            "- **Seul** — Sans source\n"
        )
    )
    assert '<strong class="entry-title">Titre &lt;b&gt;</strong> Résumé (x)' in html
    assert '<span class="entry-sources"><a href="https://a.fr/1"' in html
    assert '</a><a href="https://b.fr"' in html and " · " not in html
    assert '<strong class="entry-title">Seul</strong> Sans source</li>' in html
    assert "<b>" not in html


def test_group_by_month_and_french_parts() -> None:
    from datetime import date

    from veille.web.rendering import fr_month_year, fr_weekday, group_by_month

    days = [date(2026, 11, 8), date(2026, 11, 2), date(2026, 10, 31), date(2025, 11, 30)]
    assert group_by_month(days) == [days[:2], [days[2]], [days[3]]]
    assert group_by_month([]) == []
    assert fr_weekday(date(2026, 11, 8)) == "dimanche"
    assert fr_month_year(date(2026, 8, 1)) == "août 2026"
