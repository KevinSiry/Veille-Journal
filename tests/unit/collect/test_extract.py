from __future__ import annotations

import httpx
import pytest
import respx

from veille.collect import extract as extract_mod
from veille.collect.extract import MAX_TEXT_CHARS, TrafilaturaExtractor, _decode
from veille.collect.net_safety import SafeHttp
from veille.config import Settings
from veille.contracts import ContentExtractor, ExtractedPage

from .conftest import fixture_bytes

URL = "https://news.example/article"


@pytest.fixture
def extractor(settings: Settings, http: SafeHttp) -> TrafilaturaExtractor:
    return TrafilaturaExtractor(settings, http)


def test_is_content_extractor(extractor: TrafilaturaExtractor) -> None:
    assert isinstance(extractor, ContentExtractor)


def test_happy_path_with_redirect(
    extractor: TrafilaturaExtractor, mock_router: respx.MockRouter
) -> None:
    mock_router.get("https://short.example/a").respond(301, headers={"Location": URL})
    route = mock_router.get(URL).respond(
        200,
        content=fixture_bytes("article.html"),
        headers={"Content-Type": "text/html; charset=utf-8"},
    )
    page = extractor.extract("https://short.example/a")
    assert isinstance(page, ExtractedPage)
    assert page.final_url == URL
    assert "plan d'investissement" in page.text
    assert "Mentions légales" not in page.text
    assert "text/html" in route.calls.last.request.headers["accept"]


def test_xhtml_and_empty_extraction(
    extractor: TrafilaturaExtractor, mock_router: respx.MockRouter
) -> None:
    mock_router.get(URL).respond(
        200,
        content=b"<html><body></body></html>",
        headers={"Content-Type": "application/xhtml+xml"},
    )
    assert extractor.extract(URL) == ExtractedPage(URL, "")


@pytest.mark.parametrize("ctype", ["application/pdf", "image/png", "application/json", None])
def test_non_html_returns_none(
    extractor: TrafilaturaExtractor, mock_router: respx.MockRouter, ctype: str | None
) -> None:
    headers = {"Content-Type": ctype} if ctype else {}
    mock_router.get(URL).respond(200, content=b"%PDF-1.4", headers=headers)
    assert extractor.extract(URL) is None


@pytest.mark.parametrize(
    "setup",
    ["timeout", "connect", "404", "big", "private"],
)
def test_failures_return_none(
    extractor: TrafilaturaExtractor,
    mock_router: respx.MockRouter,
    settings: Settings,
    setup: str,
) -> None:
    url = URL
    if setup == "timeout":
        mock_router.get(url).mock(side_effect=httpx.TimeoutException("t"))
    elif setup == "connect":
        mock_router.get(url).mock(side_effect=httpx.ConnectError("c"))
    elif setup == "404":
        mock_router.get(url).respond(404, headers={"Content-Type": "text/html"})
    elif setup == "big":
        mock_router.get(url).respond(
            200, content=b"x" * (settings.max_page_bytes + 1), headers={"Content-Type": "text/html"}
        )
    else:
        url = "http://169.254.169.254/latest/meta-data/"
    assert extractor.extract(url) is None


def test_trafilatura_crash_returns_none(
    extractor: TrafilaturaExtractor, mock_router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> str:
        raise ValueError("parse error")

    monkeypatch.setattr(extract_mod.trafilatura, "extract", boom)
    mock_router.get(URL).respond(
        200, content=b"<html></html>", headers={"Content-Type": "text/html"}
    )
    assert extractor.extract(URL) is None


def test_text_capped(
    extractor: TrafilaturaExtractor, mock_router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract_mod.trafilatura, "extract", lambda *a, **k: "z" * 50_000)
    mock_router.get(URL).respond(
        200, content=b"<html></html>", headers={"Content-Type": "text/html"}
    )
    page = extractor.extract(URL)
    assert page is not None and len(page.text) == MAX_TEXT_CHARS


def test_decode() -> None:
    assert _decode("été".encode("latin-1"), "iso-8859-1") == "été"
    assert _decode("été".encode(), "unknown-charset") == "été"
    assert _decode("été".encode("cp1252"), None) == "été"
