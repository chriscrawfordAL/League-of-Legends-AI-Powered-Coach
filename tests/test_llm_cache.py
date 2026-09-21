"""Test the FMAPI response cache (#7): identical prompts hit the endpoint once."""

import os
import sys
import types

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

from analysis import coach  # noqa: E402


def _install_fake_sdk(monkeypatch, calls):
    class _Msg:
        content = "cached-response"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Endpoints:
        def query(self, **kwargs):
            calls.append(kwargs)
            return _Resp()

    class _WC:
        serving_endpoints = _Endpoints()

    sdk = types.ModuleType("databricks.sdk")
    sdk.WorkspaceClient = lambda: _WC()
    serving = types.ModuleType("databricks.sdk.service.serving")
    serving.ChatMessage = lambda **k: k
    serving.ChatMessageRole = types.SimpleNamespace(SYSTEM="system", USER="user")
    for name, mod in [
        ("databricks", types.ModuleType("databricks")),
        ("databricks.sdk", sdk),
        ("databricks.sdk.service", types.ModuleType("databricks.sdk.service")),
        ("databricks.sdk.service.serving", serving),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


def test_chat_caches_identical_prompts(monkeypatch):
    coach._LLM_CACHE.clear()
    coach._LLM_CACHE_ORDER.clear()
    calls = []
    _install_fake_sdk(monkeypatch, calls)

    a = coach._chat("ep", "system prompt", "user prompt", max_tokens=100)
    b = coach._chat("ep", "system prompt", "user prompt", max_tokens=100)
    assert a == b == "cached-response"
    assert len(calls) == 1  # second call served from cache

    # A different prompt is a cache miss -> a second endpoint call.
    coach._chat("ep", "system prompt", "different user prompt", max_tokens=100)
    assert len(calls) == 2
