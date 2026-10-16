"""SQLite 持久化层与旧 JSON 迁移的测试。"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from admin.db import Database
from admin.metrics import RequestMetrics, _new_bucket, day_key
from admin.server import Store


def credential(uid="one", name="测试账号"):
    return {
        "account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
        "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token",
                 "expiresAt": int(time.time() * 1000) + 3600000},
    }


class DatabaseTests(unittest.TestCase):
    def test_state_round_trip_and_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "mywb.db")
            try:
                data = {
                    "accounts": {"a1": {"name": "账号一", "file": "a1.info", "enabled": True, "created": 5,
                                        "proxy": "http://127.0.0.1:7897", "never_cooldown": True}},
                    "active": "a1",
                    "keys": {"k1": {"name": "客户端", "hash": "abc", "hint": "sk-…", "created": 6}},
                    "pool": {"routing": "manual", "auto_checkin": False, "checkin_time": "07:30",
                             "egress_rps": 1.5, "egress_burst": 2, "egress_max_wait": 8.0},
                    "account_status": {"a1": {"remaining": 12.5, "checkin_date": "2026-01-01"}},
                    "session_bindings": {"bind": {"account_id": "a1", "expires": 123.5}},
                }
                db.save_state(data)
                loaded = db.load_state()
                self.assertEqual(loaded["active"], "a1")
                self.assertEqual(loaded["accounts"]["a1"]["name"], "账号一")
                self.assertTrue(loaded["accounts"]["a1"]["enabled"])
                self.assertEqual(loaded["accounts"]["a1"]["proxy"], "http://127.0.0.1:7897")
                self.assertTrue(loaded["accounts"]["a1"]["never_cooldown"])
                self.assertEqual(loaded["keys"]["k1"]["hash"], "abc")
                self.assertEqual(loaded["pool"], {"routing": "manual", "auto_checkin": False, "checkin_time": "07:30",
                                                  "egress_rps": 1.5, "egress_burst": 2, "egress_max_wait": 8.0})
                self.assertEqual(loaded["account_status"]["a1"]["remaining"], 12.5)
                self.assertEqual(loaded["session_bindings"]["bind"]["expires"], 123.5)
            finally:
                db.close()

    def test_sessions_persist_and_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "mywb.db")
            try:
                self.assertEqual(db.sessions(), {})
                db.replace_sessions({"d1": {"csrf": "c1", "expires": 100.0}})
                db.replace_sessions({"d2": {"csrf": "c2", "expires": 200.0}})
                self.assertEqual(db.sessions(), {"d2": {"csrf": "c2", "expires": 200.0}})
            finally:
                db.close()

    def test_request_log_prunes_and_orders(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "mywb.db")
            try:
                for index in range(120):
                    db.add_request_log({"time": index, "path": "/v1/chat/completions", "source": "api",
                                        "status": 200, "ok": True, "duration_ms": index, "outcome": "success",
                                        "model": "m", "streaming": index % 2 == 0, "key": "客户端 A",
                                        "tokens": {"prompt": 1, "completion": 2, "total": 3, "cached": 0, "reasoning": 0}})
                db.prune_request_log(100)
                self.assertEqual(db.count_request_log(), 100)
                rows = db.request_log(10)
                self.assertEqual(rows[0]["time"], 119)
                self.assertTrue(rows[0]["streaming"] is False)
                self.assertEqual(rows[1]["time"], 118)
                self.assertEqual(rows[0]["tokens"]["total"], 3)
                self.assertEqual(rows[0]["key"], "客户端 A")
            finally:
                db.close()

    def test_metric_days_upsert_is_incremental(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "mywb.db")
            try:
                bucket = {**_new_bucket(), "requests": 4, "ttft_hist": [0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]}
                db.upsert_metric_day("2026-01-01", "all", "", bucket)
                db.upsert_metric_days({"2026-01-02": {"all": {**_new_bucket(), "requests": 2}, "models": {"m1": {**_new_bucket(), "requests": 1}}}})
                days = db.load_metric_days()
                self.assertEqual(days["2026-01-01"]["all"]["requests"], 4)
                self.assertEqual(days["2026-01-02"]["all"]["requests"], 2)
                self.assertEqual(days["2026-01-02"]["models"]["m1"]["requests"], 1)
                self.assertEqual(days["2026-01-01"]["all"]["ttft_hist"][1], 4)
                # 只保留一天，最旧的被清理。
                db.prune_metric_days(1)
                self.assertEqual(list(db.load_metric_days()), ["2026-01-02"])
            finally:
                db.close()

    def test_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mywb.db"
            db = Database(path)
            db.save_state({"accounts": {"a": {"name": "n", "file": "a.info", "enabled": True, "created": 1}},
                           "active": "a", "keys": {}, "pool": {}, "account_status": {}, "session_bindings": {}})
            db.close()
            reopened = Database(path)
            try:
                self.assertEqual(reopened.load_state()["active"], "a")
                self.assertEqual(reopened.get_meta("schema_version"), "1")
            finally:
                reopened.close()


    def test_max_concurrent_round_trip_and_legacy_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mywb.db"
            # 模拟旧库：accounts 表没有 max_concurrent 列。
            import sqlite3
            legacy = sqlite3.connect(str(path))
            legacy.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL, file TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL DEFAULT 0)")
            legacy.execute("INSERT INTO accounts (id, name, file, enabled, created) VALUES ('a1', '旧账号', 'a1.info', 1, 1)")
            legacy.commit()
            legacy.close()
            db = Database(path)
            try:
                # 迁移补齐列，旧行默认 0。
                self.assertEqual(db.load_state()["accounts"]["a1"]["max_concurrent"], 0)
                db.save_state({"accounts": {"a1": {"name": "新账号", "file": "a1.info", "enabled": True, "created": 1, "max_concurrent": 4}},
                               "active": "a1", "keys": {}, "pool": {}, "account_status": {}, "session_bindings": {}})
                self.assertEqual(db.load_state()["accounts"]["a1"]["max_concurrent"], 4)
                # 重复打开不会因重复迁移报错。
                again = Database(path)
                self.assertEqual(again.load_state()["accounts"]["a1"]["max_concurrent"], 4)
                again.close()
            finally:
                db.close()


    def test_request_log_account_round_trip_and_legacy_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mywb.db"
            # 模拟旧库：request_log 表没有 account 列。
            import sqlite3
            legacy = sqlite3.connect(str(path))
            legacy.execute("CREATE TABLE request_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, path TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'api', status INTEGER, ok INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', ttft_ms INTEGER, streaming INTEGER NOT NULL DEFAULT 0, key_label TEXT NOT NULL DEFAULT '', prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, cached_tokens INTEGER, reasoning_tokens INTEGER)")
            legacy.execute("INSERT INTO request_log (ts, path, source, status, ok, duration_ms, outcome, model) VALUES (1, '/v1/messages', 'api', 200, 1, 5, 'success', 'm')")
            legacy.commit()
            legacy.close()
            db = Database(path)
            try:
                # 迁移补齐列，旧记录账号为空串。
                rows = db.request_log(10)
                self.assertEqual(rows[0]["account"], "")
                db.add_request_log({"time": 2, "path": "/v1/responses", "source": "api", "status": 200, "ok": True,
                                    "duration_ms": 8, "outcome": "success", "model": "m", "streaming": True,
                                    "key": "客户端", "account": "账号甲",
                                    "tokens": {"prompt": 1, "completion": 2, "total": 3, "cached": 0, "reasoning": 0}})
                latest = db.request_log(1)[0]
                self.assertEqual(latest["account"], "账号甲")
                # 重复打开不会因重复迁移报错。
                again = Database(path)
                self.assertEqual(again.request_log(1)[0]["account"], "账号甲")
                again.close()
            finally:
                db.close()


class LegacyMigrationTests(unittest.TestCase):
    def test_state_json_imported_then_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "management"
            root.mkdir()
            auth = Path(temp) / "auth"
            auth.mkdir()
            legacy = {
                "accounts": {"a1": {"name": "旧账号", "file": "a1.info", "enabled": True, "created": 3}},
                "active": "a1",
                "keys": {"k1": {"name": "旧密钥", "hash": "deadbeef", "hint": "sk-…", "created": 4}},
                "pool": {"routing": "manual", "auto_checkin": True, "checkin_time": "08:15"},
                "account_status": {"a1": {"remaining": 9.5}},
                "session_bindings": {},
            }
            (root / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
            db = Database(root / "mywb.db")
            try:
                store = Store(root, auth, "initial", "admin-key-long-enough", db=db)
                self.assertFalse((root / "state.json").exists())
                self.assertEqual(store.data["active"], "a1")
                self.assertEqual(store.data["accounts"]["a1"]["name"], "旧账号")
                self.assertEqual(store.data["keys"]["k1"]["hash"], "deadbeef")
                self.assertEqual(store.data["pool"]["checkin_time"], "08:15")
                self.assertEqual(store.data["account_status"]["a1"]["remaining"], 9.5)
            finally:
                db.close()

    def test_metrics_json_imported_then_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = {"version": 1, "days": {day_key(): {"all": {**_new_bucket(), "requests": 7, "prompt_tokens": 30}, "models": {}}}}
            (root / "metrics.json").write_text(json.dumps(legacy), encoding="utf-8")
            db = Database(root / "mywb.db")
            try:
                metrics = RequestMetrics(db, legacy_path=root / "metrics.json")
                self.assertEqual(metrics.stats()["all"]["requests"], 7)
                self.assertEqual(metrics.stats()["all"]["prompt_tokens"], 30)
                self.assertFalse((root / "metrics.json").exists())
                # 重复启动不会重复导入。
                again = RequestMetrics(db, legacy_path=root / "metrics.json")
                self.assertEqual(again.stats()["all"]["requests"], 7)
            finally:
                db.close()

    def test_corrupt_legacy_state_falls_back_to_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "management"
            root.mkdir()
            auth = Path(temp) / "auth"
            auth.mkdir()
            (auth / "a1.info").write_text(json.dumps(credential("nine")), encoding="utf-8")
            (root / "state.json").write_text("{not json", encoding="utf-8")
            db = Database(root / "mywb.db")
            try:
                store = Store(root, auth, "initial", "admin-key-long-enough", db=db)
                # 损坏的 state.json 被忽略，账号从 auth/*.info 重建。
                self.assertEqual(len(store.data["accounts"]), 1)
                self.assertTrue(store.data["keys"])
            finally:
                db.close()

    def test_stale_json_removed_when_db_already_initialized(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "management"
            root.mkdir()
            auth = Path(temp) / "auth"
            auth.mkdir()
            db = Database(root / "mywb.db")
            try:
                Store(root, auth, "initial", "admin-key-long-enough", db=db)
                (root / "state.json").write_text(json.dumps({"accounts": {"stale": {}}}), encoding="utf-8")
                store = Store(root, auth, "initial", "admin-key-long-enough", db=db)
                self.assertFalse((root / "state.json").exists())
                self.assertNotIn("stale", store.data["accounts"])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
