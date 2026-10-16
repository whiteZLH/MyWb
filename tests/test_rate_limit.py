"""上游出口匀速器测试。

全部用例通过 ``rate_limit.reset(...)`` 显式设定参数，覆盖 conftest 的默认关闭。
时间断言留足容差，避免 CI 抖动导致假红。
"""

import asyncio
import time

import httpx
import pytest

from core import rate_limit
from core.rate_limit import Pacer


def test_disabled_is_noop():
    pacer = Pacer(rps=0)
    start = time.monotonic()
    for _ in range(10):
        assert pacer.acquire() is True
    assert time.monotonic() - start < 0.05


def test_paces_at_configured_rate():
    # 25 req/s -> 间隔 0.04s；4 次取槽位应耗时约 3 个间隔。
    pacer = Pacer(rps=25)
    start = time.monotonic()
    for _ in range(4):
        assert pacer.acquire() is True
    elapsed = time.monotonic() - start
    assert 0.09 <= elapsed <= 0.5, elapsed


def test_first_acquire_is_immediate():
    pacer = Pacer(rps=1)
    start = time.monotonic()
    assert pacer.acquire() is True
    assert time.monotonic() - start < 0.05


def test_burst_allows_initial_spike():
    # burst=3：前 3 次立即放行，第 4 次才等待一个间隔。
    pacer = Pacer(rps=10, burst=3)  # 间隔 0.1s
    start = time.monotonic()
    for _ in range(3):
        assert pacer.acquire() is True
    fast = time.monotonic() - start
    assert fast < 0.08, fast
    start = time.monotonic()
    assert pacer.acquire() is True
    assert time.monotonic() - start >= 0.05


def test_timeout_returns_false_without_reserving():
    pacer = Pacer(rps=2, max_wait=5.0)  # 间隔 0.5s
    assert pacer.acquire() is True  # 占用第一个槽位
    # 下一个槽位约 0.5s 后可用，0.1s 超时必须失败。
    assert pacer.acquire(timeout=0.1) is False
    # 失败不应占用槽位：随后无限等待能立即拿到刚才那个槽位（约 0.5s）。
    start = time.monotonic()
    assert pacer.acquire(timeout=2.0) is True
    assert time.monotonic() - start < 0.9


def test_configure_hot_updates():
    pacer = Pacer(rps=1, burst=1, max_wait=5.0)
    assert pacer.acquire() is True
    # 关闭后立即放行，不再等待。
    pacer.configure(rps=0)
    start = time.monotonic()
    for _ in range(5):
        assert pacer.acquire() is True
    assert time.monotonic() - start < 0.05


def test_async_acquire_disabled_is_immediate():
    async def run():
        rate_limit.reset(rps=0)
        return await rate_limit.PACER.acquire_async(timeout=1.0)

    assert asyncio.run(run()) is True


def test_reset_uses_given_value():
    rate_limit.reset(rps=0)
    assert rate_limit.PACER.enabled is False
    rate_limit.reset(rps=3)
    assert rate_limit.PACER.enabled is True
    assert rate_limit.PACER.rps == 3


def test_chat_endpoint_returns_429_when_pacer_times_out(monkeypatch):
    """排队超时必须在到达上游之前返回 HTTP 429。"""
    from core import converter

    real_client = httpx.AsyncClient

    def upstream(request):  # 到达上游即失败：证明限速在发请求前就拦下了。
        raise AssertionError("rate-limited request must not reach upstream")

    monkeypatch.setattr(
        converter.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(upstream), **kw),
    )
    monkeypatch.setattr(converter, "_check_auth", lambda *a: None)
    monkeypatch.setattr(converter, "_cred", lambda: type("Credential", (), {"get_headers": lambda self: {}})())
    monkeypatch.setattr(converter, "_log", lambda *a: None)

    async def run():
        async with real_client(transport=httpx.ASGITransport(app=converter.app), base_url="http://test") as client:
            # 1 req/s、排队上限 0.05s：先占掉当前槽位，下一个请求必然超时。
            rate_limit.reset(rps=1, burst=1, max_wait=0.05)
            assert rate_limit.PACER.acquire(timeout=0.05) is True
            result = await client.post(
                "/v1/chat/completions",
                json={"model": "test", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
            )
            assert result.status_code == 429
            assert result.json()["detail"]["error"]["type"] == "rate_limit_error"

    asyncio.run(run())
