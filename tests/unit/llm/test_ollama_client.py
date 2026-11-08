import json

import httpx
import pytest
import respx

from veille.config import Settings
from veille.contracts import LLM, LLMError, LLMUnavailableError
from veille.llm.ollama_client import EMBED_BATCH_SIZE, OllamaClient, strip_think

BASE = "http://ollama.test:11434"
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


@pytest.fixture
def settings() -> Settings:
    return Settings(ollama_url=BASE + "/", ollama_model="qwen3:8b", ollama_embed_model="bge-m3")


def chat_reply(content: str, **extra: object) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}, **extra})


@respx.mock
def test_chat_json_payload_and_usage(settings: Settings) -> None:
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=chat_reply('{"a": 1}', prompt_eval_count=100, eval_count=20)
    )
    client = OllamaClient(settings)
    assert isinstance(client, LLM)
    assert client.model == "qwen3:8b"
    assert client.chat_json("sys", "usr", SCHEMA) == {"a": 1}
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "model": "qwen3:8b",
        "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}],
        "format": SCHEMA,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_ctx": 4096},
        "keep_alive": "10m",
    }
    assert client.last_usage == 120
    client.close()


@respx.mock
def test_chat_json_strips_think_and_fences(settings: Settings) -> None:
    respx.post(f"{BASE}/api/chat").mock(
        return_value=chat_reply('<think>hmm {"no": 1}</think>\n```json\n{"a": 2}\n```')
    )
    client = OllamaClient(settings)
    assert client.chat_json("s", "u", SCHEMA) == {"a": 2}
    assert client.last_usage is None


def test_strip_think_unbalanced() -> None:
    assert strip_think('reasoning...</think>{"a": 3}') == '{"a": 3}'


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"a": '])
@respx.mock
def test_chat_json_invalid_json(settings: Settings, content: str) -> None:
    respx.post(f"{BASE}/api/chat").mock(return_value=chat_reply(content))
    with pytest.raises(LLMError) as exc:
        OllamaClient(settings).chat_json("s", "u", SCHEMA)
    assert not isinstance(exc.value, LLMUnavailableError)


@respx.mock
def test_chat_json_missing_content(settings: Settings) -> None:
    respx.post(f"{BASE}/api/chat").mock(return_value=httpx.Response(200, json={"done": True}))
    with pytest.raises(LLMError):
        OllamaClient(settings).chat_json("s", "u", SCHEMA)


@respx.mock
def test_chat_json_non_json_body(settings: Settings) -> None:
    respx.post(f"{BASE}/api/chat").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(LLMError):
        OllamaClient(settings).chat_json("s", "u", SCHEMA)


@respx.mock
def test_connect_error_is_unavailable(settings: Settings) -> None:
    respx.post(f"{BASE}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LLMUnavailableError):
        OllamaClient(settings).chat_json("s", "u", SCHEMA)


@respx.mock
def test_503_is_unavailable(settings: Settings) -> None:
    respx.post(f"{BASE}/api/chat").mock(return_value=httpx.Response(503))
    with pytest.raises(LLMUnavailableError):
        OllamaClient(settings).chat_json("s", "u", SCHEMA)


@respx.mock
def test_other_http_error_and_timeout(settings: Settings) -> None:
    route = respx.post(f"{BASE}/api/chat")
    route.mock(return_value=httpx.Response(500))
    client = OllamaClient(settings)
    with pytest.raises(LLMError) as exc:
        client.chat_json("s", "u", SCHEMA)
    assert not isinstance(exc.value, LLMUnavailableError)
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LLMError) as exc:
        client.chat_json("s", "u", SCHEMA)
    assert not isinstance(exc.value, LLMUnavailableError)
    route.mock(side_effect=httpx.RemoteProtocolError("boom"))
    with pytest.raises(LLMError):
        client.chat_json("s", "u", SCHEMA)


@respx.mock
def test_timeout_settings_and_override(settings: Settings) -> None:
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return chat_reply('{"a": 1}')

    respx.post(f"{BASE}/api/chat").mock(side_effect=handler)
    OllamaClient(settings).chat_json("s", "u", SCHEMA)
    OllamaClient(settings, timeout_s=60).chat_json("s", "u", SCHEMA)
    assert seen == [settings.llm_timeout_s, 60]


@respx.mock
def test_embed_batches_by_32(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "bge-m3" and body["keep_alive"] == "10m"
        return httpx.Response(
            200, json={"embeddings": [[float(len(t)), 1.0] for t in body["input"]]}
        )

    route = respx.post(f"{BASE}/api/embed").mock(side_effect=handler)
    texts = [f"t{'x' * i}" for i in range(EMBED_BATCH_SIZE + 5)]
    vectors = OllamaClient(settings).embed(texts)
    assert len(route.calls) == 2
    assert [len(json.loads(c.request.content)["input"]) for c in route.calls] == [32, 5]
    assert vectors[0] == [1.0, 1.0] and vectors[-1] == [float(len(texts[-1])), 1.0]


@respx.mock
def test_embed_invalid_response(settings: Settings) -> None:
    respx.post(f"{BASE}/api/embed").mock(return_value=httpx.Response(200, json={"x": 1}))
    with pytest.raises(LLMError):
        OllamaClient(settings).embed(["a"])


def test_embed_empty_makes_no_call(settings: Settings) -> None:
    with respx.mock(assert_all_called=False):
        assert OllamaClient(settings).embed([]) == []


@respx.mock
def test_list_models_and_ping(settings: Settings) -> None:
    respx.get(f"{BASE}/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "qwen3:8b"}, {"name": "bge-m3:latest"}, {}]}
        )
    )
    version = respx.get(f"{BASE}/api/version")
    version.mock(return_value=httpx.Response(200, json={"version": "0.9"}))
    client = OllamaClient(settings, client=httpx.Client())
    assert client.list_models() == ["qwen3:8b", "bge-m3:latest"]
    assert client.ping() is True
    version.mock(side_effect=httpx.ConnectError("down"))
    assert client.ping() is False
    client.close()  # injected client: not closed by us
