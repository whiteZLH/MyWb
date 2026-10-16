"""SQLite 持久化层（标准库 sqlite3，零新依赖）。

设计目标与取舍
--------------

1. **单一数据源**：``mywb.db`` 取代原先的 ``state.json`` / ``metrics.json``。
   账号、密钥摘要、账号池设置、账号状态、会话绑定、管理会话、按天统计与
   请求记录全部落在同一文件，进程重启后不丢。

2. **配置类数据用“快照同步”**：``Store.data`` 仍是一个普通 dict（现有代码与
   测试都直接读写它）。``save_state()`` 在事务里把整份快照写回各表。这类数据
   量小（账号/密钥上限各 100、绑定上限 4096），整表重写足够廉价，也让既有
   调用点无需改动。

3. **高写入数据用增量 UPSERT**：请求记录与按天统计逐条 INSERT / UPSERT，
   并带索引，这是 SQLite 相对 JSON 真正带来收益的部分。

4. **只存数字与展示名**：请求记录沿用 ``metrics.py`` 的隐私约束，绝不写入
   提示词、回复内容或完整密钥。``account_status`` 作为动态字段包以 JSON 文本
   存储，其内容本身也全是数字/状态枚举，不含凭据。

5. **线程安全**：单进程多线程（uvicorn 线程池 + ``asyncio.to_thread``）访问，
   统一由 ``self.lock`` 串行化；连接以 ``check_same_thread=False`` 共享。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

STATUS_DEFAULT = 0


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# 按天统计的桶字段，顺序即建表列顺序，便于 load/save 对称映射。
BUCKET_FIELDS = (
    "requests", "success", "http_success", "api_requests", "test_requests",
    "duration_ms", "ttft_ms", "ttft_count", "decode_ms", "decode_tokens",
    "prompt_tokens", "completion_tokens", "reasoning_tokens", "cache_hit_tokens",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    file           TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created        INTEGER NOT NULL DEFAULT 0,
    max_concurrent INTEGER NOT NULL DEFAULT 0,
    proxy          TEXT NOT NULL DEFAULT '',
    never_cooldown INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_keys (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    hash    TEXT NOT NULL,
    hint    TEXT NOT NULL,
    created INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id      TEXT PRIMARY KEY,
    link    TEXT NOT NULL,
    remark  TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pool_settings (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    routing        TEXT NOT NULL,
    auto_checkin   INTEGER NOT NULL,
    checkin_time   TEXT NOT NULL,
    egress_rps     REAL NOT NULL DEFAULT 2.0,
    egress_burst   INTEGER NOT NULL DEFAULT 1,
    egress_max_wait REAL NOT NULL DEFAULT 10.0
);

CREATE TABLE IF NOT EXISTS account_status (
    account_id TEXT PRIMARY KEY,
    data       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_bindings (
    key        TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    expires    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    digest  TEXT PRIMARY KEY,
    csrf    TEXT NOT NULL,
    expires REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_days (
    day               TEXT NOT NULL,
    scope             TEXT NOT NULL,
    model             TEXT NOT NULL,
    requests          INTEGER NOT NULL DEFAULT 0,
    success           INTEGER NOT NULL DEFAULT 0,
    http_success      INTEGER NOT NULL DEFAULT 0,
    api_requests      INTEGER NOT NULL DEFAULT 0,
    test_requests     INTEGER NOT NULL DEFAULT 0,
    duration_ms       REAL NOT NULL DEFAULT 0,
    ttft_ms           REAL NOT NULL DEFAULT 0,
    ttft_count        INTEGER NOT NULL DEFAULT 0,
    decode_ms         REAL NOT NULL DEFAULT 0,
    decode_tokens     INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens  INTEGER NOT NULL DEFAULT 0,
    ttft_hist         TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (day, scope, model)
);

CREATE TABLE IF NOT EXISTS request_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER NOT NULL,
    path              TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT 'api',
    status            INTEGER,
    ok                INTEGER NOT NULL DEFAULT 0,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    outcome           TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    ttft_ms           INTEGER,
    streaming         INTEGER NOT NULL DEFAULT 0,
    key_label         TEXT NOT NULL DEFAULT '',
    account           TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    cached_tokens     INTEGER,
    reasoning_tokens  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log (ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_metric_days_day ON metric_days (day);
"""


def _chmod_private(path: Path) -> None:
    """尽量收紧权限；Windows 上 chmod 基本无效，忽略失败。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Database:
    """单个 SQLite 文件上的所有读写。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(_SCHEMA)
            self._ensure_columns()
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()
        _chmod_private(self.path)

    def _ensure_columns(self):
        """为已存在的旧表补齐后续新增的列（``CREATE TABLE IF NOT EXISTS`` 不会加列）。"""
        migrations = {
            "accounts": {
                "max_concurrent": "INTEGER NOT NULL DEFAULT 0",
                "proxy": "TEXT NOT NULL DEFAULT ''",
                "never_cooldown": "INTEGER NOT NULL DEFAULT 0",
            },
            "request_log": {"account": "TEXT NOT NULL DEFAULT ''"},
            "pool_settings": {
                "egress_rps": "REAL NOT NULL DEFAULT 2.0",
                "egress_burst": "INTEGER NOT NULL DEFAULT 1",
                "egress_max_wait": "REAL NOT NULL DEFAULT 10.0",
            },
        }
        for table, columns in migrations.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------ meta

    def get_meta(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
            self.conn.commit()

    # -------------------------------------------------------------- accounts

    def accounts(self):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM accounts ORDER BY created, id").fetchall()
        return {
            row["id"]: {
                "name": row["name"],
                "file": row["file"],
                "enabled": bool(row["enabled"]),
                "created": row["created"],
                "max_concurrent": int(row["max_concurrent"] or 0),
                "proxy": str(row["proxy"] or ""),
                "never_cooldown": bool(row["never_cooldown"]),
            }
            for row in rows
        }

    def api_keys(self):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM api_keys ORDER BY created, id").fetchall()
        return {
            row["id"]: {
                "name": row["name"],
                "hash": row["hash"],
                "hint": row["hint"],
                "created": row["created"],
            }
            for row in rows
        }

    def nodes(self):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM nodes ORDER BY created, id").fetchall()
        return {
            row["id"]: {
                "link": row["link"],
                "remark": row["remark"],
                "enabled": bool(row["enabled"]),
                "created": row["created"],
            }
            for row in rows
        }

    def pool_settings(self):
        with self.lock:
            row = self.conn.execute("SELECT * FROM pool_settings WHERE id = 1").fetchone()
        if row is None:
            return {"routing": "round_robin", "auto_checkin": True, "checkin_time": "09:00",
                    "egress_rps": 2.0, "egress_burst": 1, "egress_max_wait": 10.0}
        return {
            "routing": row["routing"],
            "auto_checkin": bool(row["auto_checkin"]),
            "checkin_time": row["checkin_time"],
            "egress_rps": float(row["egress_rps"]),
            "egress_burst": int(row["egress_burst"]),
            "egress_max_wait": float(row["egress_max_wait"]),
        }

    def account_status(self):
        with self.lock:
            rows = self.conn.execute("SELECT account_id, data FROM account_status").fetchall()
        result = {}
        for row in rows:
            try:
                value = json.loads(row["data"])
            except ValueError:
                continue
            if isinstance(value, dict):
                result[row["account_id"]] = value
        return result

    def session_bindings(self):
        with self.lock:
            rows = self.conn.execute("SELECT key, account_id, expires FROM session_bindings").fetchall()
        return {row["key"]: {"account_id": row["account_id"], "expires": row["expires"]} for row in rows}

    def active_account(self):
        with self.lock:
            row = self.conn.execute("SELECT value FROM app_state WHERE key = 'active'").fetchone()
        return row["value"] if row else None

    # ---------------------------------------------------------- state snapshot

    def load_state(self):
        """读取整份配置快照，形状与旧 state.json 完全一致。"""
        return {
            "accounts": self.accounts(),
            "active": self.active_account(),
            "keys": self.api_keys(),
            "nodes": self.nodes(),
            "pool": self.pool_settings(),
            "account_status": self.account_status(),
            "session_bindings": self.session_bindings(),
        }

    def save_state(self, data):
        """在单个事务里把快照写回各表（整表重写，配置数据量很小）。"""
        accounts = data.get("accounts") or {}
        keys = data.get("keys") or {}
        status = data.get("account_status") or {}
        bindings = data.get("session_bindings") or {}
        pool = data.get("pool") or {"routing": "round_robin", "auto_checkin": True, "checkin_time": "09:00",
                                    "egress_rps": 2.0, "egress_burst": 1, "egress_max_wait": 10.0}
        active = data.get("active")
        with self.lock:
            try:
                self.conn.execute("BEGIN")
                self.conn.execute("DELETE FROM accounts")
                self.conn.executemany(
                    "INSERT INTO accounts (id, name, file, enabled, created, max_concurrent, proxy, never_cooldown) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (aid, str(item.get("name") or ""), str(item.get("file") or ""),
                         int(bool(item.get("enabled"))), int(item.get("created") or 0),
                         max(0, int(item.get("max_concurrent") or 0)),
                         str(item.get("proxy") or ""), int(bool(item.get("never_cooldown"))))
                        for aid, item in accounts.items()
                    ],
                )
                self.conn.execute("DELETE FROM api_keys")
                self.conn.executemany(
                    "INSERT INTO api_keys (id, name, hash, hint, created) VALUES (?, ?, ?, ?, ?)",
                    [
                        (kid, str(item.get("name") or ""), str(item.get("hash") or ""),
                         str(item.get("hint") or ""), int(item.get("created") or 0))
                        for kid, item in keys.items()
                    ],
                )
                nodes = data.get("nodes") or {}
                self.conn.execute("DELETE FROM nodes")
                self.conn.executemany(
                    "INSERT INTO nodes (id, link, remark, enabled, created) VALUES (?, ?, ?, ?, ?)",
                    [
                        (nid, str(item.get("link") or ""), str(item.get("remark") or ""),
                         int(bool(item.get("enabled", True))), int(item.get("created") or 0))
                        for nid, item in nodes.items()
                    ],
                )
                self.conn.execute("DELETE FROM account_status")
                self.conn.executemany(
                    "INSERT INTO account_status (account_id, data) VALUES (?, ?)",
                    [(aid, json.dumps(value, ensure_ascii=False)) for aid, value in status.items()],
                )
                self.conn.execute("DELETE FROM session_bindings")
                self.conn.executemany(
                    "INSERT INTO session_bindings (key, account_id, expires) VALUES (?, ?, ?)",
                    [
                        (key, str(item.get("account_id") or ""), float(item.get("expires") or 0))
                        for key, item in bindings.items()
                    ],
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO pool_settings (id, routing, auto_checkin, checkin_time, egress_rps, egress_burst, egress_max_wait) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (str(pool.get("routing") or "round_robin"), int(bool(pool.get("auto_checkin", True))),
                     str(pool.get("checkin_time") or "09:00"),
                     _as_float(pool.get("egress_rps"), 2.0), _as_int(pool.get("egress_burst"), 1),
                     _as_float(pool.get("egress_max_wait"), 10.0)),
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO app_state (key, value) VALUES ('active', ?)",
                    (None if active is None else str(active),),
                )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise

    # -------------------------------------------------------------- sessions

    def sessions(self):
        with self.lock:
            rows = self.conn.execute("SELECT digest, csrf, expires FROM sessions").fetchall()
        return {row["digest"]: {"csrf": row["csrf"], "expires": row["expires"]} for row in rows}

    def replace_sessions(self, sessions):
        with self.lock:
            try:
                self.conn.execute("BEGIN")
                self.conn.execute("DELETE FROM sessions")
                self.conn.executemany(
                    "INSERT INTO sessions (digest, csrf, expires) VALUES (?, ?, ?)",
                    [(key, str(item.get("csrf") or ""), float(item.get("expires") or 0)) for key, item in sessions.items()],
                )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise

    # --------------------------------------------------------- metric days

    def load_metric_days(self):
        """返回 ``{day: {"all": bucket, "models": {name: bucket}, "accounts": {name: bucket}}}``。"""
        with self.lock:
            rows = self.conn.execute("SELECT * FROM metric_days").fetchall()
        days = {}
        for row in rows:
            day = days.setdefault(row["day"], {"all": None, "models": {}, "accounts": {}})
            bucket = _bucket_from_row(row)
            if row["scope"] == "all":
                day["all"] = bucket
            elif row["scope"] == "account":
                day["accounts"][row["model"]] = bucket
            else:
                day["models"][row["model"]] = bucket
        for day in days.values():
            if day["all"] is None:
                day["all"] = _empty_row_bucket()
        return days

    def upsert_metric_day(self, day, scope, model, bucket):
        with self.lock:
            self._write_day(day, scope, model, bucket)
            self.conn.commit()

    def upsert_metric_days(self, days):
        """批量 UPSERT 若干天，不触碰未列出的天（增量落盘用）。"""
        with self.lock:
            try:
                self.conn.execute("BEGIN")
                for day, value in days.items():
                    self._write_day(day, "all", "", value.get("all") or {})
                    for name, bucket in (value.get("models") or {}).items():
                        self._write_day(day, "model", name, bucket)
                    for name, bucket in (value.get("accounts") or {}).items():
                        self._write_day(day, "account", name, bucket)
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise

    def _write_day(self, day, scope, model, bucket):
        columns = ", ".join(BUCKET_FIELDS)
        placeholders = ", ".join("?" for _ in BUCKET_FIELDS)
        values = [bucket.get(field, STATUS_DEFAULT) for field in BUCKET_FIELDS]
        hist = bucket.get("ttft_hist")
        hist_text = json.dumps([int(count) for count in hist]) if isinstance(hist, list) else "[]"
        self.conn.execute(
            f"INSERT OR REPLACE INTO metric_days (day, scope, model, {columns}, ttft_hist) "
            f"VALUES (?, ?, ?, {placeholders}, ?)",
            [day, scope, model, *values, hist_text],
        )

    def replace_metric_days(self, days):
        with self.lock:
            try:
                self.conn.execute("BEGIN")
                self.conn.execute("DELETE FROM metric_days")
                for day, value in days.items():
                    self._write_day(day, "all", "", value.get("all") or {})
                    for name, bucket in (value.get("models") or {}).items():
                        self._write_day(day, "model", name, bucket)
                    for name, bucket in (value.get("accounts") or {}).items():
                        self._write_day(day, "account", name, bucket)
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise

    def prune_metric_days(self, keep_days):
        """只保留最近 ``keep_days`` 个日历日（按 day 字符串排序）。"""
        with self.lock:
            rows = self.conn.execute("SELECT DISTINCT day FROM metric_days ORDER BY day").fetchall()
            stale = [row["day"] for row in rows[:-keep_days]] if len(rows) > keep_days else []
            if stale:
                self.conn.executemany("DELETE FROM metric_days WHERE day = ?", [(day,) for day in stale])
                self.conn.commit()

    # ---------------------------------------------------------- request log

    def add_request_log(self, record):
        tokens = record.get("tokens") or {}
        with self.lock:
            self.conn.execute(
                "INSERT INTO request_log (ts, path, source, status, ok, duration_ms, outcome, model, "
                "ttft_ms, streaming, key_label, account, prompt_tokens, completion_tokens, total_tokens, "
                "cached_tokens, reasoning_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(record.get("time") or 0),
                    str(record.get("path") or ""),
                    str(record.get("source") or "api"),
                    record.get("status"),
                    int(bool(record.get("ok"))),
                    int(record.get("duration_ms") or 0),
                    str(record.get("outcome") or ""),
                    str(record.get("model") or ""),
                    record.get("ttft_ms"),
                    int(bool(record.get("streaming"))),
                    str(record.get("key") or ""),
                    str(record.get("account") or ""),
                    tokens.get("prompt"),
                    tokens.get("completion"),
                    tokens.get("total"),
                    tokens.get("cached"),
                    tokens.get("reasoning"),
                ),
            )
            self.conn.commit()

    def request_log(self, limit=100):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM request_log ORDER BY ts DESC, id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def prune_request_log(self, keep):
        """只保留最新 ``keep`` 条，其余删除。"""
        with self.lock:
            self.conn.execute(
                "DELETE FROM request_log WHERE id NOT IN (SELECT id FROM request_log ORDER BY ts DESC, id DESC LIMIT ?)",
                (int(keep),),
            )
            self.conn.commit()

    def count_request_log(self):
        with self.lock:
            return int(self.conn.execute("SELECT COUNT(*) AS n FROM request_log").fetchone()["n"])


def _bucket_from_row(row):
    bucket = {field: row[field] for field in BUCKET_FIELDS}
    try:
        hist = json.loads(row["ttft_hist"])
    except (ValueError, TypeError):
        hist = []
    bucket["ttft_hist"] = hist if isinstance(hist, list) else []
    return bucket


def _empty_row_bucket():
    bucket = {field: 0 for field in BUCKET_FIELDS}
    bucket["ttft_hist"] = []
    return bucket


def _record_from_row(row):
    tokens = None
    if row["total_tokens"] is not None or row["prompt_tokens"] is not None or row["completion_tokens"] is not None:
        tokens = {
            "prompt": row["prompt_tokens"] or 0,
            "completion": row["completion_tokens"] or 0,
            "total": row["total_tokens"] or 0,
            "cached": row["cached_tokens"] or 0,
            "reasoning": row["reasoning_tokens"] or 0,
        }
    return {
        "time": row["ts"],
        "path": row["path"],
        "source": row["source"],
        "status": row["status"],
        "ok": bool(row["ok"]),
        "duration_ms": row["duration_ms"],
        "outcome": row["outcome"],
        "model": row["model"],
        "ttft_ms": row["ttft_ms"],
        "streaming": bool(row["streaming"]),
        "key": row["key_label"],
        "account": row["account"],
        "tokens": tokens,
    }
