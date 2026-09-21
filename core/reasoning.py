"""
reasoning.py — 上游思考内容（reasoning / thinking）字段识别与提取。

不同后端/模型对“思考过程”的字段命名不一致：
  - DeepSeek:     delta.reasoning_content  (字符串)
  - 部分网关:      delta.reasoning          (字符串)
  - 部分网关:      delta.thinking           (字符串)
  - 部分网关:      message.reasoning_content / message.reasoning
  - 部分网关:      delta.reasoning_content 为对象 {"content": "..."} 或列表

本模块只负责从 delta / message 中稳定取出“增量思考文本”，
供三条协议链路（chat / responses / messages）复用到各自的响应格式。
"""

from __future__ import annotations

import os
from typing import Any

# 增量字段优先级：越靠前越可信
_DELTA_REASONING_KEYS = (
    "reasoning_content",
    "reasoning",
    "thinking",
    "thinking_content",
    "reasoning_text",
)


def reasoning_join_enabled() -> bool:
    """思考内容换行规整开关：默认开启（CODEBUDDY_REASONING_JOIN=0 关闭）。

    上游（DeepSeek 等）常在 reasoning_content 里输出 `\\n\\n` 空行分段，
    部分客户端（Codex / 各类 OpenAI 兼容 GUI）会据此把思考渲染成多个独立
    段落/卡片。规整后把空白折叠为单个空格，让思考合并成连续一段。
    """
    return os.environ.get("CODEBUDDY_REASONING_JOIN", "1") != "0"


def strip_empty_toolcalls_enabled() -> bool:
    """剥离空 tool_calls 字段的开关：默认开启（CODEBUDDY_STRIP_EMPTY_TOOLCALLS=0 关闭）。

    上游（腾讯 CodeBuddy）每个 chat SSE delta 都带 `"tool_calls":[]`（空数组）。
    部分客户端打包的旧版 Vercel AI SDK 用 `tool_calls != null` 判断工具调用
    （空数组恒为真），于是每收到一个 reasoning token 就关闭并重开一次
    reasoning 块 —— 表现为 opencode 里思考被切成每 token 一个 "Thought" 卡片。
    转发时删除空 tool_calls，仅在真有工具调用时保留。
    """
    return os.environ.get("CODEBUDDY_STRIP_EMPTY_TOOLCALLS", "1") != "0"


class ReasoningJoiner:
    """有状态地把流式思考片段里的换行/空行规整为单个空格。

    上游逐 token 下发 reasoning_content，跨 chunk 的空行可能被拆开
    （如上一 chunk 以 `\\n` 结尾、下一 chunk 以 `\\n` 开头），因此需要
    暂存结尾的空白，等后续非空白字符到达时再决定是否补一个空格。
    """

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._pending_ws = False  # 已读入空白、等待下一个非空白字符
        self._started = False     # 是否已输出过非空白内容

    @property
    def enabled(self) -> bool:
        return self._enabled

    def feed(self, text: str) -> str:
        """输入一段增量思考文本，返回规整后的文本（可能为空）。"""
        if not text:
            return ""
        if not self._enabled:
            return text
        out: list[str] = []
        for ch in text:
            if ch in " \t\r\n\f\v":
                self._pending_ws = True
                continue
            if self._pending_ws and self._started:
                out.append(" ")
            self._pending_ws = False
            self._started = True
            out.append(ch)
        return "".join(out)

    def flush(self) -> str:
        """流结束时清空挂起状态（尾部空白直接丢弃）。"""
        self._pending_ws = False
        return ""


def normalize_reasoning_text(text: str) -> str:
    """非流式场景：一次性把思考文本里的换行/连续空白折叠为单个空格。"""
    if not text:
        return ""
    return " ".join(text.split())


def _coerce_text(value: Any) -> str:
    """把各种形态的思考字段值统一成字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        # {"content": "..."} / {"text": "..."} / {"summary": "..."}
        for key in ("content", "text", "summary", "reasoning_content"):
            if key in value:
                return _coerce_text(value[key])
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "summary"):
                    if isinstance(item.get(key), str):
                        parts.append(item[key])
                        break
        return "".join(parts)
    return ""


def extract_reasoning_delta(delta: Any) -> str:
    """从 delta（或 message）对象中取出本片段的思考文本增量。

    返回空字符串表示该片段不含思考内容。
    """
    if not isinstance(delta, dict):
        return ""
    for key in _DELTA_REASONING_KEYS:
        if key in delta and delta[key] is not None:
            text = _coerce_text(delta[key])
            if text:
                return text
    # 嵌套形态：{"reasoning": {"content": "..."}}
    return ""


def has_reasoning_delta(delta: Any) -> bool:
    """判断该片段是否携带思考内容（用于决定是否开启/关闭 thinking 块）。"""
    return bool(extract_reasoning_delta(delta))
