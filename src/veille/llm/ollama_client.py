"""HTTP client for a local Ollama server, implementing ``veille.contracts.LLM``."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from veille.config import Settings
from veille.contracts import LLMError, LLMUnavailableError

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 32
KEEP_ALIVE = "10m"
CHAT_OPTIONS: dict[str, Any] = {"temperature": 0.2, "num_ctx": 4096}

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)

# One call in flight per process: the CPU budget of the VPS cannot afford more.
_PROCESS_LOCK = threading.Lock()


def strip_think(text: str) -> str:
    """Remove ``<think>...</think>`` blocks (and a dangling opening one) and code fences."""
    cleaned = _THINK_RE.sub("", text)
    if "</think>" in cleaned.lower():  # unbalanced: keep what follows the last closing tag
        cleaned = cleaned[cleaned.lower().rfind("</think>") + len("</think>") :]
    cleaned = cleaned.strip()
    fence = _FENCE_RE.match(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    return cleaned


class OllamaClient:
    """Synchronous Ollama client (``/api/chat``, ``/api/embed``, ``/api/tags``)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.model: str = settings.ollama_model
        self.embed_model = settings.ollama_embed_model
        self.base_url = settings.ollama_url.rstrip("/")
        self.timeout_s = timeout_s if timeout_s is not None else settings.llm_timeout_s
        self._client = client if client is not None else httpx.Client()
        self._owns_client = client is None
        self.last_usage: int | None = None

    # ------------------------------------------------------------------ helpers

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        with _PROCESS_LOCK:
            try:
                response = self._client.request(method, url, json=payload, timeout=self.timeout_s)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise LLMUnavailableError(f"Ollama injoignable ({url}) : {exc}") from exc
            except httpx.TimeoutException as exc:
                # A read timeout means the model is busy (CPU-bound generation): callers
                # treat it like 503, i.e. retrying or going on is pointless for now.
                raise LLMUnavailableError(f"Ollama occupé : délai dépassé ({path})") from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"Erreur réseau Ollama ({path}) : {exc}") from exc
        if response.status_code == 503:
            raise LLMUnavailableError("Ollama est occupé ou indisponible (503)")
        if response.status_code >= 400:
            raise LLMError(f"Erreur HTTP Ollama {response.status_code} ({path})")
        try:
            return response.json()
        except ValueError as exc:
            raise LLMError(f"Réponse Ollama illisible ({path})") from exc

    # ------------------------------------------------------------------ LLM API

    def chat_json(self, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": dict(schema),
            "stream": False,
            "think": False,
            "options": dict(CHAT_OPTIONS),
            "keep_alive": KEEP_ALIVE,
        }
        self.last_usage = None
        data = self._request("POST", "/api/chat", payload)
        if not isinstance(data, dict):
            raise LLMError("Réponse Ollama inattendue")
        usage = [data.get("prompt_eval_count"), data.get("eval_count")]
        counts = [u for u in usage if isinstance(u, int)]
        self.last_usage = sum(counts) if counts else None
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise LLMError("Réponse Ollama sans contenu")
        try:
            parsed = json.loads(strip_think(content))
        except ValueError as exc:
            raise LLMError("JSON invalide renvoyé par le modèle") from exc
        if not isinstance(parsed, dict):
            raise LLMError("Le modèle n'a pas renvoyé un objet JSON")
        return parsed

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        result: list[list[float]] = []
        items = list(texts)
        for start in range(0, len(items), EMBED_BATCH_SIZE):
            batch = items[start : start + EMBED_BATCH_SIZE]
            payload = {"model": self.embed_model, "input": batch, "keep_alive": KEEP_ALIVE}
            data = self._request("POST", "/api/embed", payload)
            vectors = data.get("embeddings") if isinstance(data, dict) else None
            if not isinstance(vectors, list) or len(vectors) != len(batch):
                raise LLMError("Réponse d'embeddings Ollama invalide")
            result.extend([float(x) for x in vec] for vec in vectors)
        return result

    # --------------------------------------------------------------- diagnostics

    def list_models(self) -> list[str]:
        data = self._request("GET", "/api/tags")
        models = data.get("models", []) if isinstance(data, dict) else []
        return [str(m["name"]) for m in models if isinstance(m, dict) and "name" in m]

    def ping(self) -> bool:
        try:
            self._request("GET", "/api/version")
        except LLMError:
            return False
        return True
