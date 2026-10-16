"""pytest 全局配置。

默认禁用 .env 自动加载，保证测试结果不受开发者本机 .env 影响：
每个用例都以“干净环境 + 显式传参”为起点。需要验证 .env 逻辑的用例
（tests/test_settings.py）会显式调用 ``settings.load_env_file(path=...)``。

默认关闭上游出口匀速器（``CODEBUDDY_EGRESS_RPS=0``）：避免人为拖慢用例，
并让并发/时序断言保持确定。需要验证限速行为的用例（tests/test_rate_limit.py）
自行调用 ``rate_limit.reset(...)`` 覆盖。
"""
import os

os.environ.setdefault("MYWB_SKIP_DOTENV", "1")
os.environ.setdefault("CODEBUDDY_EGRESS_RPS", "0")

import pytest


@pytest.fixture(autouse=True)
def _disable_egress_pacer():
    from core import rate_limit

    rate_limit.reset(rps=0)
    yield
    rate_limit.reset(rps=0)
