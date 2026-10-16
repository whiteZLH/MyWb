"""上游出口匀速器（进程级单例）。

背景：目标平台按**出口 IP** 做速率风控，同一 IP 上聚合请求达到约每秒 3 个
就会触发 WAF/429。因此需要在网关侧把**全部上游请求的总速率**摊开、压到阈值
之下——这与"每账号并发上限"是两条独立的线。

本模块提供一个跨全部上游请求共享的"漏桶 + 最小间隔"匀速器：每个请求发出前
取一个槽位，槽位按 ``1/rps`` 秒的固定间隔依次放行；``burst`` 允许空闲后一次
突发若干槽位（默认 1，即严格匀速）。

设计取舍：

- 零新依赖；``core`` 不依赖 ``admin``。
- 全局单例 ``PACER``。管理后台在启动与改配置时调用 ``PACER.configure(...)``
  热更新；独立转换器模式用环境变量（.env）兜底。
- ``rps <= 0`` 表示关闭（``acquire`` 立即放行），用于测试与显式关停。
- ``acquire`` 是阻塞式的（供同步调用点：token 刷新 / 模型目录 / 积分查询）；
  async 调用点用 ``acquire_async``（内部 ``asyncio.to_thread``，不阻塞事件循环）。
- ``key`` 参数当前只有一个默认桶；保留该形参是为将来"按账号代理分桶"（每个
  出口 IP 一份配额）预留，本期不实现代理。
"""

from __future__ import annotations

import asyncio
import threading
import time

from . import settings

ENV_RPS = "CODEBUDDY_EGRESS_RPS"
ENV_BURST = "CODEBUDDY_EGRESS_BURST"
ENV_MAX_WAIT = "CODEBUDDY_EGRESS_MAX_WAIT"

# 触发点约 3 req/s，默认留余量取 2。
DEFAULT_RPS = 2.0
DEFAULT_BURST = 1
DEFAULT_MAX_WAIT = 10.0


def _as_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class Pacer:
    """漏桶 + 最小间隔匀速器。

    - ``rps <= 0`` 关闭；``burst`` 为允许的突发槽位数（>=1）。
    - ``acquire(timeout)`` 阻塞等待，取到返回 True，超时未取到返回 False；
      ``timeout=None`` 表示一直等待。
    """

    def __init__(self, rps=DEFAULT_RPS, burst=DEFAULT_BURST, max_wait=DEFAULT_MAX_WAIT):
        self._lock = threading.Lock()
        self._rps = 0.0
        self._burst = 1
        self._max_wait = DEFAULT_MAX_WAIT
        # 下一个可用槽位的单调时刻；0.0 表示"立即空闲"。
        self._next = 0.0
        self.configure(rps, burst, max_wait)

    def configure(self, rps=None, burst=None, max_wait=None) -> None:
        """热更新参数；重新配置后重置节奏，避免沿用旧间隔造成一次长等待。"""
        with self._lock:
            if rps is not None:
                self._rps = max(0.0, _as_float(rps, 0.0))
            if burst is not None:
                self._burst = max(1, _as_int(burst, 1))
            if max_wait is not None:
                self._max_wait = max(0.0, _as_float(max_wait, DEFAULT_MAX_WAIT))
            self._next = 0.0

    @property
    def rps(self) -> float:
        return self._rps

    @property
    def burst(self) -> int:
        return self._burst

    @property
    def max_wait(self) -> float:
        return self._max_wait

    @property
    def enabled(self) -> bool:
        return self._rps > 0

    def snapshot(self) -> dict:
        with self._lock:
            return {"rps": self._rps, "burst": self._burst, "max_wait": self._max_wait}

    def acquire(self, timeout=None, key=None) -> bool:
        """阻塞式取槽位；返回是否成功（超时返回 False）。

        在锁内完成"预留"（推进 ``_next``），在锁外睡眠，避免长时间持锁。
        """
        with self._lock:
            if self._rps <= 0:
                return True
            interval = 1.0 / self._rps
            now = time.monotonic()
            # burst 通过允许起点落在"过去"来实现：空闲足够久后可立即发 burst 个。
            floor = now - (self._burst - 1) * interval
            start = self._next if self._next > floor else floor
            wait = start - now
            if timeout is not None and wait > timeout:
                # 不预留，直接失败，避免占用一个没人用的槽位。
                return False
            self._next = start + interval
        if wait > 0:
            time.sleep(wait)
        return True

    async def acquire_async(self, timeout=None, key=None) -> bool:
        return await asyncio.to_thread(self.acquire, timeout, key)


def _env_config() -> tuple[float, int, float]:
    return (
        _as_float(settings.get(ENV_RPS), DEFAULT_RPS),
        _as_int(settings.get(ENV_BURST), DEFAULT_BURST),
        _as_float(settings.get(ENV_MAX_WAIT), DEFAULT_MAX_WAIT),
    )


# 进程级单例：默认取环境变量（.env）兜底值；管理后台会热覆盖。
PACER = Pacer(*_env_config())


def reset(rps=None, burst=None, max_wait=None) -> None:
    """测试用：重置为指定值（缺省回落到环境变量 / 默认值）。"""
    env_rps, env_burst, env_wait = _env_config()
    PACER.configure(
        rps=env_rps if rps is None else rps,
        burst=env_burst if burst is None else burst,
        max_wait=env_wait if max_wait is None else max_wait,
    )


def configure(rps=None, burst=None, max_wait=None) -> None:
    """供管理后台热更新（语义同 ``PACER.configure``，缺省不覆盖）。"""
    PACER.configure(rps=rps, burst=burst, max_wait=max_wait)
