"""Run history (read-only) and the health check."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from veille.contracts import Run, Store
from veille.web.deps import StoreDep
from veille.web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter()
public_router = APIRouter()

_STAT_FIELDS = (
    "collected",
    "new",
    "clusters",
    "kept",
    "dropped",
    "llm_calls",
    "tokens_estimated",
    "duration_s",
)


def _theme_rows(stats: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalise ``runs.stats`` JSON (RunStats.to_json_dict) into display rows."""
    themes = stats.get("themes")
    if not isinstance(themes, list | tuple):
        return []
    rows: list[dict[str, Any]] = []
    for item in themes:
        if not isinstance(item, Mapping):
            continue
        row: dict[str, Any] = {
            "theme_name": str(item.get("theme_name", item.get("theme_id", "?"))),
            "status": str(item.get("status", "")),
            "error": str(item["error"]) if item.get("error") else "",
        }
        for key in _STAT_FIELDS:
            value = item.get(key, 0)
            row[key] = value if isinstance(value, int | float) else 0
        rows.append(row)
    return rows


@router.get("/admin/runs")
def runs(request: Request, store: StoreDep) -> Response:
    entries = [{"run": run, "themes": _theme_rows(run.stats)} for run in store.list_runs(20)]
    return render(request, "runs.html", {"entries": entries})


def _run_json(run: Run | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": str(run.status),
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@public_router.get("/healthz")
def healthz(request: Request) -> Response:
    factory: Callable[[], Store] = request.app.state.store_factory
    db_ok = False
    last_run: dict[str, Any] | None = None
    try:
        store = factory()
    except Exception:
        logger.exception("Health check: cannot open the store")
    else:
        try:
            db_ok = bool(store.ping())
            if db_ok:
                last_run = _run_json(store.last_run())
        except Exception:
            logger.exception("Health check: store error")
            db_ok = False
        finally:
            store.close()
    body = {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "last_run": last_run,
        "version": request.app.state.settings.version,
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)
