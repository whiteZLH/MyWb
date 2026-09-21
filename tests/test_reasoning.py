#!/usr/bin/env python3
"""
test_reasoning.py — 验证三条协议链路对上游思考内容（reasoning）的兼容。

覆盖：
  - reasoning.extract_reasoning_delta 字段识别
  - chat/completions 非流式聚合 → message.reasoning_content
  - Responses API → reasoning output item + summary 事件 + reasoning_tokens
  - Anthropic Messages → thinking content block

直接运行：python3 tests/test_reasoning.py
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")

from core.reasoning import (
    extract_reasoning_delta,
    has_reasoning_delta,
    ReasoningJoiner,
    normalize_reasoning_text,
    reasoning_join_enabled,
    strip_empty_toolcalls_enabled,
)
from core.responses_adapter import ResponsesStreamConverter
from core.anthropic_adapter import AnthropicStreamConverter, anthropic_request_to_chat
from core.converter import _collect_stream, UpstreamSSERewriter


# ---------------------------------------------------------------------------
# reasoning.py
# ---------------------------------------------------------------------------

def test_extract_field_variants():
    assert extract_reasoning_delta({"reasoning_content": "思考A"}) == "思考A"
    assert extract_reasoning_delta({"reasoning": "思考B"}) == "思考B"
    assert extract_reasoning_delta({"thinking": "思考C"}) == "思考C"
    assert extract_reasoning_delta({"reasoning": {"content": "嵌套"}}) == "嵌套"
    assert extract_reasoning_delta({"reasoning": [{"text": "列表"}]}) == "列表"
    assert extract_reasoning_delta({"content": "普通内容"}) == ""
    assert extract_reasoning_delta(None) == ""
    assert has_reasoning_delta({"reasoning_content": "x"}) is True
    assert has_reasoning_delta({"content": "x"}) is False
    print("✅ test_extract_field_variants")


# ---------------------------------------------------------------------------
# 思考换行规整（ReasoningJoiner / normalize_reasoning_text）
# ---------------------------------------------------------------------------

def test_normalize_reasoning_text():
    assert normalize_reasoning_text("a\n\nb  c") == "a b c"
    assert normalize_reasoning_text("  前导 尾随  ") == "前导 尾随"
    assert normalize_reasoning_text("") == ""
    print("✅ test_normalize_reasoning_text")


def test_joiner_folds_cross_chunk_newlines():
    """跨 chunk 的空行（上片以 \\n 结尾、下片以 \\n 开头）也要折叠为单个空格。"""
    j = ReasoningJoiner(True)
    out = "".join(
        j.feed(x) for x in ["We need", " answer.\n", "\nProof:", " suppose\n\nagain", ". done"]
    )
    assert out == "We need answer. Proof: suppose again. done", repr(out)
    assert "\n" not in out
    print("✅ test_joiner_folds_cross_chunk_newlines")


def test_joiner_disabled_passthrough():
    j = ReasoningJoiner(False)
    out = "".join(j.feed(x) for x in ["a\n", "\nb", " c"])
    assert out == "a\n\nb c", repr(out)
    print("✅ test_joiner_disabled_passthrough")


def test_reasoning_join_enabled_default(monkeypatch):
    monkeypatch.delenv("CODEBUDDY_REASONING_JOIN", raising=False)
    assert reasoning_join_enabled() is True
    monkeypatch.setenv("CODEBUDDY_REASONING_JOIN", "0")
    assert reasoning_join_enabled() is False
    monkeypatch.setenv("CODEBUDDY_REASONING_JOIN", "1")
    assert reasoning_join_enabled() is True
    print("✅ test_reasoning_join_enabled_default")


def test_strip_empty_toolcalls_enabled_default(monkeypatch):
    monkeypatch.delenv("CODEBUDDY_STRIP_EMPTY_TOOLCALLS", raising=False)
    assert strip_empty_toolcalls_enabled() is True
    monkeypatch.setenv("CODEBUDDY_STRIP_EMPTY_TOOLCALLS", "0")
    assert strip_empty_toolcalls_enabled() is False
    print("✅ test_strip_empty_toolcalls_enabled_default")


# ---------------------------------------------------------------------------
# chat/completions 非流式聚合
# ---------------------------------------------------------------------------

class _FakeStream:
    """把预置 SSE 行伪装成 httpx.Response.aiter_lines()。"""

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def test_collect_stream_keeps_reasoning():
    lines = [
        'data: {"model":"deepseek-v4.1-flash","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"让我想想…"}}]}',
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":" 答案是 42。"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"答案是 42"}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12}}',
        "data: [DONE]",
    ]
    result = asyncio.run(_collect_stream(_FakeStream(lines)))
    msg = result["choices"][0]["message"]
    assert msg["content"] == "答案是 42", msg
    assert msg["reasoning_content"] == "让我想想… 答案是 42。", msg
    print("✅ test_collect_stream_keeps_reasoning")


def test_collect_stream_folds_reasoning_newlines():
    """非流式聚合后，思考里的空行被折叠为空格（不出现 \\n\\n 分段）。"""
    lines = [
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"第一段\\n\\n第二段"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"答案"}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    result = asyncio.run(_collect_stream(_FakeStream(lines)))
    msg = result["choices"][0]["message"]
    assert msg["reasoning_content"] == "第一段 第二段", msg
    print("✅ test_collect_stream_folds_reasoning_newlines")


def test_upstream_rewriter_folds_and_preserves_framing():
    """chat 流式透传：改写 reasoning 换行，但不破坏 SSE 事件分隔与统计。"""
    r = UpstreamSSERewriter(join_reasoning=True)
    raw = (
        b'data: {"choices":[{"delta":{"reasoning_content":"a\\n\\nb"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: [DONE]\n\n'
    )
    out = r.feed(raw)
    assert b'"reasoning_content":"a b"' in out, out
    # 空行分隔必须完整保留（每个 data: 行以 \n\n 收尾）
    assert out.count(b"\n\n") == 4, out
    assert r.reasoning_chars == 4, r.reasoning_chars
    assert r.finish_reason == "stop", r.finish_reason
    print("✅ test_upstream_rewriter_folds_and_preserves_framing")


def test_upstream_rewriter_cross_chunk_split():
    """\\n\\n 被拆到两个 chunk 时，规整仍需正确（跨 chunk 状态）。"""
    r = UpstreamSSERewriter(join_reasoning=True)
    out1 = r.feed(b'data: {"choices":[{"delta":{"reasoning_content":"a\\n"}}]}\n\n')
    out2 = r.feed(b'data: {"choices":[{"delta":{"reasoning_content":"\\nb"}}]}\n\n')
    full = out1 + out2
    assert full.count(b"\n\n") == 2, full
    assert b'"reasoning_content":"a"' in full and b'"reasoning_content":" b"' in full, full
    print("✅ test_upstream_rewriter_cross_chunk_split")


def test_upstream_rewriter_disabled_passthrough():
    r = UpstreamSSERewriter(join_reasoning=False)
    out = r.feed(b'data: {"choices":[{"delta":{"reasoning_content":"a\\n\\nb"}}]}\n\n')
    assert b'"reasoning_content":"a\\n\\nb"' in out, out
    print("✅ test_upstream_rewriter_disabled_passthrough")


def test_upstream_rewriter_strips_empty_toolcalls():
    """空 tool_calls（[]）必须被剥离：旧版 AI SDK 会误判为工具调用而切断思考块。"""
    r = UpstreamSSERewriter(join_reasoning=True, strip_empty_toolcalls=True)
    raw = (
        b'data: {"choices":[{"delta":{"reasoning_content":"The","tool_calls":[]}}]}\n\n'
        b'data: {"choices":[{"delta":{"reasoning_content":" user","tool_calls":[]}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"hi","tool_calls":[]}}]}\n\n'
    )
    out = r.feed(raw)
    assert b'"tool_calls"' not in out, out
    assert b'"reasoning_content":"The"' in out, out
    assert b'"reasoning_content":" user"' in out, out
    print("✅ test_upstream_rewriter_strips_empty_toolcalls")


def test_upstream_rewriter_keeps_real_toolcalls():
    """真正的工具调用必须原样保留，并记录工具名。"""
    r = UpstreamSSERewriter(join_reasoning=True, strip_empty_toolcalls=True)
    raw = (
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"read_file","arguments":"{}"}}]}}]}\n\n'
    )
    out = r.feed(raw)
    assert b'"tool_calls"' in out, out
    assert b'"name":"read_file"' in out, out
    assert r.tool_names == ["read_file"], r.tool_names
    print("✅ test_upstream_rewriter_keeps_real_toolcalls")


def test_upstream_rewriter_strip_disabled():
    r = UpstreamSSERewriter(join_reasoning=False, strip_empty_toolcalls=False)
    out = r.feed(b'data: {"choices":[{"delta":{"reasoning_content":"a","tool_calls":[]}}]}\n\n')
    assert b'"tool_calls":[]' in out, out
    print("✅ test_upstream_rewriter_strip_disabled")


def test_collect_stream_without_reasoning():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    result = asyncio.run(_collect_stream(_FakeStream(lines)))
    msg = result["choices"][0]["message"]
    assert msg["content"] == "hi"
    assert "reasoning_content" not in msg
    print("✅ test_collect_stream_without_reasoning")


# ---------------------------------------------------------------------------
# Responses API
# ---------------------------------------------------------------------------

def test_responses_emits_reasoning_item():
    conv = ResponsesStreamConverter(model="deepseek-v4.1-flash")
    out = ""
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{"reasoning_content":"先分析"}}]}')
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{"reasoning_content":"再作答"}}]}')
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{"content":"答案"}}]}')
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}')
    out += conv.finish()

    # 事件层面
    assert "response.reasoning_summary_text.delta" in out
    assert "response.reasoning_summary_text.done" in out
    assert "response.output_item.added" in out

    resp = conv.get_nonstream_response()
    items = resp["output"]
    assert items[0]["type"] == "reasoning", items
    assert items[0]["summary"][0]["text"] == "先分析再作答", items
    assert items[1]["type"] == "message"
    assert items[1]["content"][0]["text"] == "答案"
    assert resp["usage"]["output_tokens_details"]["reasoning_tokens"] > 0, resp["usage"]
    print("✅ test_responses_emits_reasoning_item")


def test_responses_folds_reasoning_newlines():
    conv = ResponsesStreamConverter(model="m")
    conv.feed_line('data: {"choices":[{"index":0,"delta":{"reasoning_content":"先分析\\n\\n"}}]}')
    conv.feed_line('data: {"choices":[{"index":0,"delta":{"reasoning_content":"再作答"}}]}')
    conv.feed_line('data: {"choices":[{"index":0,"delta":{"content":"答案"}}]}')
    conv.feed_line('data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    resp = conv.get_nonstream_response()
    reasoning_item = resp["output"][0]
    assert reasoning_item["type"] == "reasoning", resp["output"]
    assert reasoning_item["summary"][0]["text"] == "先分析 再作答", reasoning_item
    print("✅ test_responses_folds_reasoning_newlines")


def test_responses_reasoning_disabled(monkeypatch):
    monkeypatch.setenv("CODEBUDDY_REASONING", "0")
    conv = ResponsesStreamConverter(model="m")
    conv.feed_line('data: {"choices":[{"index":0,"delta":{"reasoning_content":"不该出现"}}]}')
    conv.feed_line('data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}')
    conv.feed_line('data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    resp = conv.get_nonstream_response()
    types = [i["type"] for i in resp["output"]]
    assert "reasoning" not in types, types
    print("✅ test_responses_reasoning_disabled")


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------

def test_anthropic_emits_thinking_block():
    conv = AnthropicStreamConverter(model="deepseek-v4.1-flash")
    out = ""
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{"reasoning_content":"推理中"}}]}')
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{"content":"结果"}}]}')
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    out += conv.finish()

    assert '"type": "thinking"' in out
    assert "thinking_delta" in out
    assert "signature_delta" in out

    resp = conv.get_nonstream_response()
    blocks = resp["content"]
    assert blocks[0]["type"] == "thinking", blocks
    assert blocks[0]["thinking"] == "推理中"
    assert blocks[1]["type"] == "text"
    assert blocks[1]["text"] == "结果"
    print("✅ test_anthropic_emits_thinking_block")


def test_anthropic_folds_thinking_newlines():
    conv = AnthropicStreamConverter(model="m")
    out = ""
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{"reasoning_content":"推理\\n\\n"}}]}')
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{"reasoning_content":"继续"}}]}')
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{"content":"结果"}}]}')
    out += conv.feed_line('data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    out += conv.finish()
    resp = conv.get_nonstream_response()
    assert resp["content"][0]["type"] == "thinking", resp["content"]
    assert resp["content"][0]["thinking"] == "推理 继续", resp["content"][0]
    print("✅ test_anthropic_folds_thinking_newlines")


def test_anthropic_thinking_param_to_reasoning_effort():
    chat = anthropic_request_to_chat({
        "model": "m",
        "max_tokens": 100,
        "thinking": {"type": "enabled", "budget_tokens": 20000},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert chat["reasoning_effort"] == "high", chat.get("reasoning_effort")


def test_anthropic_thinking_disabled_no_effort():
    chat = anthropic_request_to_chat({
        "model": "m",
        "max_tokens": 100,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert "reasoning_effort" not in chat
    print("✅ test_anthropic_thinking_param")


if __name__ == "__main__":
    import os

    class _RestoreOnExit:
        """直接运行时替代 pytest 的 monkeypatch fixture。"""

        def setenv(self, key, value):
            os.environ[key] = value

        def delenv(self, key, raising=True):
            os.environ.pop(key, None)

    test_extract_field_variants()
    test_normalize_reasoning_text()
    test_joiner_folds_cross_chunk_newlines()
    test_joiner_disabled_passthrough()
    test_reasoning_join_enabled_default(_RestoreOnExit())
    test_strip_empty_toolcalls_enabled_default(_RestoreOnExit())
    test_upstream_rewriter_folds_and_preserves_framing()
    test_upstream_rewriter_cross_chunk_split()
    test_upstream_rewriter_disabled_passthrough()
    test_upstream_rewriter_strips_empty_toolcalls()
    test_upstream_rewriter_keeps_real_toolcalls()
    test_upstream_rewriter_strip_disabled()
    test_collect_stream_keeps_reasoning()
    test_collect_stream_folds_reasoning_newlines()
    test_collect_stream_without_reasoning()
    test_responses_emits_reasoning_item()
    test_responses_folds_reasoning_newlines()
    # 关闭开关的单测（不影响其它用例，跑完恢复）
    os.environ["CODEBUDDY_REASONING"] = "0"
    try:
        test_responses_reasoning_disabled(_RestoreOnExit())
    finally:
        os.environ.pop("CODEBUDDY_REASONING", None)
    test_anthropic_emits_thinking_block()
    test_anthropic_folds_thinking_newlines()
    test_anthropic_thinking_param_to_reasoning_effort()
    test_anthropic_thinking_disabled_no_effort()
    print("\n🎉 All reasoning tests passed!")
