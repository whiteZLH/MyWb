"""pytest 全局配置。

默认禁用 .env 自动加载，保证测试结果不受开发者本机 .env 影响：
每个用例都以“干净环境 + 显式传参”为起点。需要验证 .env 逻辑的用例
（tests/test_settings.py）会显式调用 ``settings.load_env_file(path=...)``。
"""
import os

os.environ.setdefault("MYWB_SKIP_DOTENV", "1")
