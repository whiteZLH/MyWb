"""统一配置层（零依赖）。

此前 .env 只由本地启动脚本读取并注入进程环境变量，导致 `python -m admin.server`
这类直接启动方式完全读不到 .env。本模块在 Python 侧补上这一层，使两种启动
方式行为一致。

取值规则（从高到低）：

    1. 显式参数（函数 / 命令行传入）
    2. 进程环境变量
    3. .env 文件
    4. 默认值 —— 一律以“当前运行目录”（cwd）为基准，而不是硬编码的 /data/*

.env 查找顺序：

    1. ``$MYWB_ENV_FILE`` 指定的文件（显式覆盖，便于多实例 / 测试隔离）
    2. ``<cwd>/.env``

设计取舍：

- 不引入 python-dotenv，requirements.txt 仍只有 fastapi / uvicorn / httpx。
- .env 的值只在“进程环境变量缺失”时写入 ``os.environ``，因此
  “环境变量 > .env”天然成立；显式参数由各调用点用 ``param or get(...)`` 保证。
- .env 中出现的相对路径一律相对 cwd 解析，避免“相对 .env 所在目录”的歧义。
- 解析规则：跳过空行与 ``#`` 注释、支持 ``export KEY=VALUE``、
  去掉值两侧成对的引号；不做变量展开，保证行为可预测。
- 进程内只加载一次；``MYWB_SKIP_DOTENV=1`` 可彻底关闭（测试用）。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE_VAR = "MYWB_ENV_FILE"
SKIP_VAR = "MYWB_SKIP_DOTENV"

_QUOTES = ("'", '"')

# 解析出的 .env 键值（首次加载后填充，供 get() 在环境变量缺失时回落）
_values: dict[str, str] = {}
_source: Path | None = None
_attempted = False


def cwd() -> Path:
    """当前运行目录，所有路径类默认值的锚点。"""
    return Path.cwd()


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本（跳过空行与注释、支持 export、去掉成对引号）。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in _QUOTES and value[-1] == value[0]:
            value = value[1:-1]
        values[name] = value
    return values


def env_file() -> Path | None:
    """定位 .env；找不到返回 None。"""
    explicit = os.environ.get(ENV_FILE_VAR, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    candidate = cwd() / ".env"
    return candidate if candidate.is_file() else None


def load_env_file(path: str | os.PathLike | None = None, override: bool = False) -> dict[str, str]:
    """把 .env 读进 ``os.environ``，仅在变量缺失时写入（除非 override=True）。

    幂等：重复调用不会重复解析。返回本次解析出的原始键值。

    ``MYWB_SKIP_DOTENV`` 只拦截“自动发现”路径；显式传入 path 时始终加载，
    便于测试在隔离环境中直接验证解析逻辑。
    """
    global _values, _source, _attempted
    if path is None and os.environ.get(SKIP_VAR, "").strip() not in ("", "0"):
        _attempted = True
        return {}
    target = Path(path).expanduser() if path is not None else env_file()
    if target is None or not target.is_file():
        _attempted = True
        return {}
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError:
        _attempted = True
        return {}
    values = parse_env_text(text)
    for name, value in values.items():
        if override or not os.environ.get(name):
            os.environ[name] = value
    _values, _source, _attempted = values, target, True
    return dict(values)


def ensure_loaded() -> None:
    """惰性加载：任何读取点首次取值时自动补齐 .env。"""
    if not _attempted:
        load_env_file()


def reset() -> None:
    """清空加载状态，供测试或多实例（重新指定 MYWB_ENV_FILE）复用。"""
    global _values, _source, _attempted
    _values, _source, _attempted = {}, None, False


def source() -> Path | None:
    """已加载的 .env 路径（便于启动日志与排障）。"""
    ensure_loaded()
    return _source


def get(name: str, default: str | None = None) -> str | None:
    """环境变量 > .env > 默认值；空字符串与未设置等价。"""
    ensure_loaded()
    value = os.environ.get(name)
    if value:
        return value
    value = _values.get(name)
    if value:
        return value
    return default


def get_bool(name: str, default: bool = False) -> bool:
    value = get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def get_int(name: str, default: int) -> int:
    value = get(name)
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def resolve_path(value: str | os.PathLike | None, base: Path | None = None) -> Path | None:
    """相对路径基于 base（默认 cwd）解析；None 透传。"""
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base or cwd()) / path


def path(name: str, default: str | os.PathLike | None = None) -> Path | None:
    """按统一优先级取路径值，并把相对路径解析到 cwd。"""
    value = get(name)
    if value is None:
        value = default
    return resolve_path(value)
