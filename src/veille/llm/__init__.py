"""LLM clients (Ollama over HTTP, deterministic fake for tests)."""

from veille.llm.fake import FakeLLM
from veille.llm.ollama_client import OllamaClient

__all__ = ["FakeLLM", "OllamaClient"]
