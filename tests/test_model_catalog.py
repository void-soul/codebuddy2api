"""`/v1/models` 改为转发后端权威模型目录（issue：/v1/models 漏掉 space-bunny 等新模型）。

背景：本地 `product.json` 与源码里的 `DEFAULT_MODELS` 都会过期 —— 前者取决于 WorkBuddy
装在哪、后者是手写快照，而后端 `/v2/enterprises/personal/models` 才是客户端 UI 用的那份
（asar 里的 `remote.models`），新模型只会在那里出现。

回落链：后端 → 本机 product.json → 硬编码 DEFAULT_MODELS。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import converter  # noqa: E402

# 后端返回体（结构取自真实响应：{"code":0,"data":{"models":[...]}}）
BACKEND_PAYLOAD = {
    "code": 0,
    "msg": "OK",
    "requestId": "test-req",
    "data": {
        "models": [
            {"id": "auto", "name": "Auto", "tags": ["craft"], "vendor": "f"},
            {"id": "hy3", "name": "混元3", "tags": ["chat"], "vendor": "f"},
            {"id": "space-bunny", "name": "Space Bunny", "tags": ["chat"], "vendor": "f"},
        ]
    },
}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """替换 httpx.Client：记录调用次数，按预设返回或抛异常。"""

    def __init__(self, payload=None, status_code=200, error=None, tracker=None):
        self._payload = payload
        self._status = status_code
        self._error = error
        self._tracker = tracker if tracker is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self._tracker.append(url)
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._payload, self._status)


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个用例都从空缓存开始（模块级缓存不能跨用例串味）。

    用 getattr 而不是直接取属性：这样在实现落地前，用例失败的原因是
    「行为不对」而不是「属性不存在」—— 前者才说明测试真的在测东西。
    """
    cache = getattr(converter, "_MODEL_CACHE", None)
    if isinstance(cache, dict):
        cache.clear()
    yield
    if isinstance(cache, dict):
        cache.clear()


@pytest.fixture(autouse=True)
def _no_local_product_json(monkeypatch):
    """默认：本机找不到 product.json（让回落链落到硬编码那一步）。"""
    monkeypatch.setattr(converter, "_find_workbuddy_product_json", lambda: None)


def _patch_backend(monkeypatch, tracker, **kwargs):
    monkeypatch.setattr(converter.httpx, "Client", lambda *a, **k: _FakeClient(tracker=tracker, **kwargs))


def _patch_credential(monkeypatch):
    """凭据只需要能给出 headers，不碰真实 auth 文件。"""

    class _Cred:
        def get_headers(self):
            return {"Authorization": "Bearer test", "X-User-Id": "u1"}

    monkeypatch.setattr(converter, "_cred", lambda: _Cred())


def test_model_catalog_prefers_backend_over_everything(monkeypatch):
    """后端列表是权威的：要含 space-bunny，且不受本地清单/硬编码快照影响。"""
    tracker = []
    _patch_backend(monkeypatch, tracker, payload=BACKEND_PAYLOAD)
    _patch_credential(monkeypatch)

    models = converter.get_available_models()

    assert "space-bunny" in models
    assert models == ["auto", "hy3", "space-bunny"]
    assert tracker, "应当真的请求了后端，而不是直接用本地快照"


def test_model_catalog_falls_back_to_product_json_when_backend_fails(monkeypatch):
    """后端挂了要退到本机 product.json，而不是直接给硬编码快照。"""
    _patch_backend(monkeypatch, [], status_code=500)
    _patch_credential(monkeypatch)

    product = Path(__file__).resolve().parent / "_fake_product.json"
    product.write_text(
        json.dumps({"models": [{"id": "from-product-json"}, {"id": "auto"}]}),
        encoding="utf-8",
    )
    try:
        monkeypatch.setattr(converter, "_find_workbuddy_product_json", lambda: product)
        models = converter.get_available_models()
    finally:
        product.unlink(missing_ok=True)

    assert "from-product-json" in models
    assert "space-bunny" not in models


def test_model_catalog_falls_back_to_hardcoded_when_all_sources_fail(monkeypatch):
    """后端与 product.json 都不可用时，仍要返回一个可用列表（服务不能挂）。"""
    _patch_backend(monkeypatch, [], error=RuntimeError("network down"))
    _patch_credential(monkeypatch)

    models = converter.get_available_models()

    assert models == converter.DEFAULT_MODELS


def test_model_catalog_keeps_non_chat_models_out(monkeypatch):
    """上游列表同样要过过滤：图像/视频模型与 vendor=tencent 内部模型不能出现在聊天模型里。"""
    payload = {
        "code": 0,
        "data": {
            "models": [
                {"id": "hy3", "vendor": "f"},
                {"id": "hunyuan-image-alpha", "tags": ["text-to-image"], "vendor": "f"},
                {"id": "hunyuan-7b-dense", "vendor": "tencent"},
                {"id": "codewise-jump", "vendor": "f"},
                {"id": "space-bunny", "vendor": "f"},
            ]
        },
    }
    _patch_backend(monkeypatch, [], payload=payload)
    _patch_credential(monkeypatch)

    models = converter.get_available_models()

    assert models == ["hy3", "space-bunny"]


def test_model_catalog_ignores_malformed_backend_payload(monkeypatch):
    """后端返回结构变了（没有 data.models）不能抛异常 —— 退回本地来源即可。"""
    _patch_backend(monkeypatch, [], payload={"code": 0, "data": {}})
    _patch_credential(monkeypatch)

    models = converter.get_available_models()

    assert models == converter.DEFAULT_MODELS


def test_model_catalog_caches_backend_result(monkeypatch):
    """连续查询只打一次后端：客户端（Claude Code 等）会反复拉模型列表。"""
    tracker = []
    _patch_backend(monkeypatch, tracker, payload=BACKEND_PAYLOAD)
    _patch_credential(monkeypatch)

    first = converter.get_available_models()
    second = converter.get_available_models()

    assert first == second == ["auto", "hy3", "space-bunny"]
    assert len(tracker) == 1, f"缓存未生效，后端被请求了 {len(tracker)} 次"
