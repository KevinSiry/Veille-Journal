"""Jinja2 environment and the page rendering helper."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jinja2
from fastapi import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from veille.web.auth import is_authenticated
from veille.web.rendering import jinja_filters
from veille.web.security import get_csrf_token

TEMPLATES_DIR = Path(__file__).parent / "templates"


def build_templates(tz: ZoneInfo) -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(jinja_filters(tz))
    return Jinja2Templates(env=env)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    templates: Jinja2Templates = request.app.state.templates
    ctx: dict[str, Any] = {
        "csrf_token": get_csrf_token(request),
        "version": request.app.state.settings.version,
        "authenticated": is_authenticated(request),
        "path": request.url.path,
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)
