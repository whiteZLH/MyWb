"""统一配置层（core.settings）与 .env 读取规则的回归测试。

覆盖：解析规则、优先级（环境变量 > .env > 默认值）、cwd 默认锚点、
以及 auth_dirs() / create_app() 两条路径的默认值行为。
"""
import os
from pathlib import Path

import pytest

from core import converter, settings

ENV_KEYS = (
    "ADMIN_DIR",
    "MANAGEMENT_DATA_DIR",
    "CODEBUDDY_AUTH_DIR",
    "CODEBUDDY2OPENAI_KEY",
    "ADMIN_KEY",
    "ADMIN_SECURE_COOKIE",
    "PORT",
    "MYWB_ENV_FILE",
    "MYWB_SKIP_DOTENV",
)


@pytest.fixture
def isolated_env():
    """快照并还原 os.environ，同时清空 settings 的加载状态。"""
    snapshot = dict(os.environ)
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    settings.reset()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
        settings.reset()


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_env_text_matches_run_ps1_rules():
    values = settings.parse_env_text(
        "\n".join(
            [
                "# comment",
                "",
                "  ",
                "ADMIN_KEY=abc123",
                "export CODEBUDDY2OPENAI_KEY=sk-wb-xyz",
                'QUOTED="with spaces"',
                "SINGLE='single quoted'",
                "EMPTY=",
                "NO_EQUALS",
                "HASH_IN_VALUE=value#not-comment",
            ]
        )
    )
    assert values["ADMIN_KEY"] == "abc123"
    assert values["CODEBUDDY2OPENAI_KEY"] == "sk-wb-xyz"
    assert values["QUOTED"] == "with spaces"
    assert values["SINGLE"] == "single quoted"
    assert values["EMPTY"] == ""
    assert values["HASH_IN_VALUE"] == "value#not-comment"
    assert "NO_EQUALS" not in values


def test_load_env_file_does_not_override_process_env(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "from-process")
    env_path = write_env(tmp_path, "ADMIN_KEY=from-file\nPORT=9999\n")
    settings.load_env_file(path=env_path)
    # 进程环境变量优先于 .env
    assert settings.get("ADMIN_KEY") == "from-process"
    assert os.environ["ADMIN_KEY"] == "from-process"
    # .env 中的其它变量正常注入
    assert settings.get("PORT") == "9999"


def test_load_env_file_injects_missing_vars(isolated_env, tmp_path):
    env_path = write_env(tmp_path, "ADMIN_DIR=/tmp/data\nCODEBUDDY_AUTH_DIR=./auth\n")
    loaded = settings.load_env_file(path=env_path)
    assert loaded == {"ADMIN_DIR": "/tmp/data", "CODEBUDDY_AUTH_DIR": "./auth"}
    assert os.environ["ADMIN_DIR"] == "/tmp/data"
    assert settings.source() == env_path


def test_load_env_file_override_flag(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "from-process")
    env_path = write_env(tmp_path, "ADMIN_KEY=from-file\n")
    settings.load_env_file(path=env_path, override=True)
    assert settings.get("ADMIN_KEY") == "from-file"


def test_skip_dotenv_blocks_auto_discovery_but_not_explicit(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("MYWB_SKIP_DOTENV", "1")
    monkeypatch.chdir(tmp_path)
    write_env(tmp_path, "ADMIN_KEY=auto\n")
    assert settings.load_env_file() == {}
    assert settings.get("ADMIN_KEY") is None
    # 显式指定路径时，跳过开关不生效
    assert settings.load_env_file(path=tmp_path / ".env") == {"ADMIN_KEY": "auto"}


def test_env_file_follows_cwd_and_explicit_override(isolated_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert settings.env_file() is None
    write_env(tmp_path, "PORT=1\n")
    assert settings.env_file() == tmp_path / ".env"

    other = tmp_path / "custom.env"
    other.write_text("PORT=2\n", encoding="utf-8")
    monkeypatch.setenv("MYWB_ENV_FILE", str(other))
    assert settings.env_file() == other


def test_get_priority_and_defaults(isolated_env, tmp_path, monkeypatch):
    assert settings.get("MISSING", "fallback") == "fallback"
    assert settings.get("MISSING") is None
    # 空字符串与未设置等价，落到 .env 或默认值
    monkeypatch.setenv("ADMIN_DIR", "")
    write_env(tmp_path, "ADMIN_DIR=from-file\n")
    settings.load_env_file(path=tmp_path / ".env")
    assert settings.get("ADMIN_DIR") == "from-file"
    assert settings.get("ADMIN_DIR", "fallback") == "from-file"


@pytest.mark.parametrize(
    "raw,expected",
    [("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True), ("0", False), ("false", False), ("", False)],
)
def test_get_bool(isolated_env, monkeypatch, raw, expected):
    if raw:
        monkeypatch.setenv("FLAG", raw)
    assert settings.get_bool("FLAG") is expected


def test_get_int_ignores_invalid(isolated_env, monkeypatch):
    monkeypatch.setenv("PORT", "not-a-number")
    assert settings.get_int("PORT", 8787) == 8787
    monkeypatch.setenv("PORT", " 9000 ")
    assert settings.get_int("PORT", 8787) == 9000
    assert settings.get_int("ABSENT", 8787) == 8787


def test_path_resolves_relative_to_cwd(isolated_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # 未设置且无默认值时透传 None（cwd 回落由调用方决定）
    assert settings.path("ADMIN_DIR") is None
    assert settings.path("ADMIN_DIR", default=settings.cwd()) == tmp_path
    monkeypatch.setenv("ADMIN_DIR", "data")
    assert settings.path("ADMIN_DIR") == tmp_path / "data"
    monkeypatch.setenv("ADMIN_DIR", str(tmp_path / "abs"))
    assert settings.path("ADMIN_DIR") == tmp_path / "abs"


def test_auth_dirs_defaults_to_cwd_and_keeps_desktop_fallback(isolated_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dirs = converter.auth_dirs()
    assert dirs[0] == tmp_path / "auth"
    # 桌面端自动发现能力不能丢
    assert len(dirs) > 1
    assert any("CodeBuddyExtension" in str(d) for d in dirs)


def test_auth_dirs_explicit_config_wins_alone(isolated_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEBUDDY_AUTH_DIR", "custom-auth")
    assert converter.auth_dirs() == [tmp_path / "custom-auth"]


def test_resolve_store_paths_defaults_to_cwd(isolated_env, tmp_path, monkeypatch):
    """未传参且无 .env 时：数据目录 = cwd，auth 目录 = cwd/auth。"""
    import admin.server as admin_server

    monkeypatch.chdir(tmp_path)
    data_dir, auth = admin_server.resolve_store_paths()
    assert Path(data_dir) == tmp_path
    assert Path(auth) == tmp_path / "auth"


def test_resolve_store_paths_reads_dotenv(isolated_env, tmp_path, monkeypatch):
    """显式参数 > .env；.env 中相对路径锚定 cwd。"""
    import admin.server as admin_server

    monkeypatch.chdir(tmp_path)
    env_path = write_env(tmp_path, "ADMIN_DIR=console-data\nCODEBUDDY_AUTH_DIR=console-auth\n")
    settings.load_env_file(path=env_path)
    data_dir, auth = admin_server.resolve_store_paths()
    assert Path(data_dir) == tmp_path / "console-data"
    assert Path(auth) == tmp_path / "console-auth"
    # 显式参数仍然高于 .env
    data_dir, auth = admin_server.resolve_store_paths(root=tmp_path / "explicit", auth_dir=tmp_path / "explicit-auth")
    assert Path(data_dir) == tmp_path / "explicit"
    assert Path(auth) == tmp_path / "explicit-auth"


def test_resolve_store_paths_process_env_beats_dotenv(isolated_env, tmp_path, monkeypatch):
    import admin.server as admin_server

    monkeypatch.chdir(tmp_path)
    env_path = write_env(tmp_path, "ADMIN_DIR=from-file\n")
    settings.load_env_file(path=env_path)
    monkeypatch.setenv("ADMIN_DIR", "from-process")
    data_dir, _ = admin_server.resolve_store_paths()
    assert Path(data_dir) == tmp_path / "from-process"


def test_management_data_dir_is_legacy_alias(isolated_env, tmp_path, monkeypatch):
    import admin.server as admin_server

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGEMENT_DATA_DIR", "legacy-data")
    data_dir, _ = admin_server.resolve_store_paths()
    assert Path(data_dir) == tmp_path / "legacy-data"


def test_create_app_autodiscovers_cwd_dotenv(isolated_env, tmp_path, monkeypatch):
    """回归：`python -m admin.server` 不经 run.ps1 也应读到 cwd/.env。"""
    import admin.server as admin_server

    monkeypatch.chdir(tmp_path)
    write_env(tmp_path, "ADMIN_DIR=auto-data\nCODEBUDDY_AUTH_DIR=auto-auth\n")
    settings.reset()
    # 不显式调用 load_env_file，模拟直接启动进程
    data_dir, auth = admin_server.resolve_store_paths()
    assert Path(data_dir) == tmp_path / "auto-data"
    assert Path(auth) == tmp_path / "auto-auth"
    assert settings.source() == tmp_path / ".env"


def test_create_app_secure_cookie_follows_env(isolated_env, tmp_path, monkeypatch):
    """secure_cookie 默认关闭；ADMIN_SECURE_COOKIE=1 时开启。"""
    import admin.server as admin_server

    monkeypatch.chdir(tmp_path)
    auth = tmp_path / "auth"
    auth.mkdir()

    app = admin_server.create_app(tmp_path / "management", auth, "client-key", "admin-key-long-enough")
    assert app.state.secure_cookie is False
    app.state.db.close()

    monkeypatch.setenv("ADMIN_SECURE_COOKIE", "1")
    app = admin_server.create_app(tmp_path / "management", auth, "client-key", "admin-key-long-enough")
    assert app.state.secure_cookie is True
    app.state.db.close()
