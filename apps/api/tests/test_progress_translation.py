import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import progress_translation as module
from app.qwen import QwenSettings


def test_detection_preserves_technical_names():
    assert module.needs_translation("Thinking…")
    assert module.needs_translation("Now: the operation catalog.")
    assert module.needs_translation("正在检查 available definitions")
    for text in ["检查 STEP 与 G54", "mcp__cnc_read_catalog OP10", "`read the file`"]:
        assert not module.needs_translation(text)


def test_cache_deduplication_and_non_thinking_request(monkeypatch):
    calls = []

    async def handler(request):
        body = json.loads(request.content)
        assert body["enable_thinking"] is False
        assert body["stream"] is False
        assert body["temperature"] == 0
        assert body["messages"][1]["content"] == "Read the catalog."
        calls.append(body)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"choices": [{"message": {"content": "正在检查工序目录。"}}]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(module, "load_qwen_settings", lambda: QwenSettings("test", "https://example.invalid", "test", 6))

    async def run():
        translator = module.ProgressTranslator()
        values = await asyncio.gather(*(translator.translate("Read the catalog.") for _ in range(6)))
        assert values == ["正在检查工序目录。"] * 6
        assert await translator.translate("Read the catalog.") == values[0]
        assert await translator.translate("检查 G54") == "检查 G54"
        assert len(calls) == 1
        assert not translator.inflight

    asyncio.run(run())


def test_failure_cooldown_missing_credentials(monkeypatch):
    monkeypatch.setattr(module, "load_qwen_settings", lambda: QwenSettings("", "https://example.invalid", "test", 6))

    async def run():
        translator = module.ProgressTranslator()
        assert await translator.translate("Read the catalog.") is None
        assert await translator.translate("Read the catalog.") is None
        assert len(translator.cache) == 1
        assert not translator.inflight

    asyncio.run(run())


@pytest.mark.parametrize("payload,status", [
    ({"choices": [{"message": {"content": "Read the catalog."}}]}, 200),
    ({"choices": [{"message": {"content": "检查 read the catalog"}}]}, 200),
    ({"choices": [{"finish_reason": "length", "message": {"content": "检查目录"}}]}, 200),
    ({"choices": []}, 200),
    ({"error": "unavailable"}, 503),
])
def test_unusable_responses_are_cached_as_failure(monkeypatch, payload, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=payload)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(module, "load_qwen_settings", lambda: QwenSettings("test", "https://example.invalid", "test", 6))

    async def run():
        translator = module.ProgressTranslator()
        assert await translator.translate("Read the catalog.") is None
        assert await translator.translate("Read the catalog.") is None
        assert len(calls) == 1
        assert not translator.inflight

    asyncio.run(run())


def test_model_concurrency_is_bounded(monkeypatch):
    active = maximum = 0

    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "检查工序目录"}}]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(module, "load_qwen_settings", lambda: QwenSettings("test", "https://example.invalid", "test", 6))

    async def run():
        translator = module.ProgressTranslator()
        values = await asyncio.gather(*(translator.translate(f"Read catalog {i}") for i in range(12)))
        assert values == ["检查工序目录"] * 12
        assert maximum == 2

    asyncio.run(run())


def test_endpoint_limits_and_chinese_passthrough():
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        assert client.post("/api/process-studio/translate", json={"texts": ["检查 G54"]}).json() == {"translations": ["检查 G54"]}
        for texts in [[], ["x"] * 7, ["x" * 2401], [" "]]:
            assert client.post("/api/process-studio/translate", json={"texts": texts}).status_code == 422


@pytest.mark.parametrize("value,expected", [
    ('{"待翻译文本":"正在检查工序目录。"}', "正在检查工序目录。"),
    ('```json\n{"translation":"正在检查工序目录。"}\n```', "正在检查工序目录。"),
    ("待翻译文本：正在检查工序目录。", "正在检查工序目录。"),
    ("译文：正在检查 STEP 模型与 G54。", "正在检查 STEP 模型与 G54。"),
    ("翻译不可用", None),
    ("（本段中文转换暂不可用）", None),
    ("请提供待翻译文本", None),
    ('{"待翻译文本":"Read the catalog."}', None),
    ('{"unexpected":"待翻译文本"}', None),
    ("正在计算加工路径。", "正在计算加工路径。"),
])
def test_response_cleaning(value, expected):
    assert module.clean_translation(value) == expected
