import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx

import admin.server as admin_server
from core import rate_limit

ADMIN = "admin-test-credential-long-enough"
API = "client-existing-key"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"}, "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token", "expiresAt": int(time.time() * 1000) + 3600000}}


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.make_app()

    def make_app(self):
        previous = getattr(self, "app", None)
        if previous is not None:
            previous.state.db.close()
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.app.state.db.close()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(result.status_code, 200)
        cookie = result.headers["set-cookie"].lower()
        for flag in ["httponly", "samesite=strict", "path=/admin"]:
            self.assertIn(flag, cookie)
        self.client.headers["X-CSRF-Token"] = result.json()["csrf"]

    async def test_access_boundaries(self):
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 401)
        self.assertEqual((await self.client.get("/admin/api/overview", headers={"Authorization": "Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models")).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization": "Bearer " + API})).status_code, 200)
        self.assertEqual((await self.client.get("/health")).json(), {"status": "ok"})
        await self.login()
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual((await self.client.post("/admin/api/keys", json={"name": "bad"})).status_code, 403)

    async def test_account_import_switch_disable_delete_and_restart(self):
        await self.login()
        first = (await self.client.get("/admin/api/overview")).json()["accounts"][0]
        result = await self.client.post("/admin/api/accounts", json={"name": "Second", "credential": credential("two")})
        self.assertEqual(result.status_code, 200)
        aid = result.json()["id"]
        self.assertTrue(first["active"])
        result = await self.client.patch("/admin/api/accounts/" + aid, json={"active": True})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(admin_server.converter.CONFIG["cred"].path.name, aid + ".info")
        await self.client.patch("/admin/api/accounts/" + aid, json={"enabled": False})
        self.assertIsNone(admin_server.converter.CONFIG["cred"])
        self.assertEqual((await self.client.patch("/admin/api/accounts/" + aid, json={"active": True})).status_code, 400)
        await self.client.patch("/admin/api/accounts/" + first["id"], json={"active": True, "name": "Primary"})
        await self.client.delete("/admin/api/accounts/" + aid)
        self.assertTrue(list((self.root / "management/trash").glob("*/credential.json")))
        await self.client.aclose()
        self.make_app()
        await self.login()
        accounts = (await self.client.get("/admin/api/overview")).json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertTrue(accounts[0]["active"])
        self.assertEqual(accounts[0]["name"], "Primary")

    async def test_key_revocation_persists_and_no_secret_leak(self):
        await self.login()
        overview = await self.client.get("/admin/api/overview")
        for forbidden in [API, "private-access-token", "private-refresh-token", '"hash"']:
            self.assertNotIn(forbidden, overview.text)
        old = overview.json()["keys"][0]["id"]
        self.assertEqual((await self.client.delete("/admin/api/keys/" + old)).status_code, 400)
        result = await self.client.post("/admin/api/keys", json={"name": "New"})
        newkey = result.json()["key"]
        await self.client.delete("/admin/api/keys/" + old)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + newkey})).status_code, 200)
        # 明文密钥绝不落库：库里只有摘要。
        stored = json.dumps(self.app.state.store.db.load_state())
        self.assertNotIn(newkey, stored)
        self.assertNotIn("private-access-token", stored)
        await self.client.aclose()
        self.make_app()
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + API})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/models", headers={"Authorization":"Bearer " + newkey})).status_code, 200)

    async def test_invalid_credentials_and_limits(self):
        await self.login()
        for doc in [{}, {"auth": {}, "account": {}}, {**credential(), "account": {"uid": "bad\r\nheader"}}, {**credential(), "auth": {**credential()["auth"], "expiresAt": "not-time"}}]:
            r = await self.client.post("/admin/api/accounts", json={"credential": doc})
            self.assertEqual(r.status_code, 400, r.text)
        r = await self.client.post("/admin/api/accounts", content=b"x" * (1024 * 1024 + 1))
        self.assertEqual(r.status_code, 413)
        r = await self.client.get("/admin/assets/unknown.js")
        self.assertEqual(r.status_code, 404)

    async def test_max_concurrent_validation_and_persistence(self):
        await self.login()
        aid = (await self.client.get("/admin/api/overview")).json()["accounts"][0]["id"]
        for bad in ["2", -1, 1001, True, 1.5]:
            r = await self.client.patch("/admin/api/accounts/" + aid, json={"max_concurrent": bad})
            self.assertEqual(r.status_code, 400, r.text)
        r = await self.client.patch("/admin/api/accounts/" + aid, json={"max_concurrent": 3})
        self.assertEqual(r.status_code, 200)
        account = (await self.client.get("/admin/api/overview")).json()["accounts"][0]
        self.assertEqual(account["max_concurrent"], 3)
        await self.client.aclose()
        self.make_app()
        await self.login()
        account = (await self.client.get("/admin/api/overview")).json()["accounts"][0]
        self.assertEqual(account["max_concurrent"], 3)

    async def test_pool_egress_settings_validation_and_hot_apply(self):
        await self.login()
        for bad in [True, -1, 51, "2"]:
            r = await self.client.patch("/admin/api/pool/settings", json={"egress_rps": bad})
            self.assertEqual(r.status_code, 400, r.text)
        for bad in [0, 21, True]:
            r = await self.client.patch("/admin/api/pool/settings", json={"egress_burst": bad})
            self.assertEqual(r.status_code, 400, r.text)
        for bad in [-1, 121, True]:
            r = await self.client.patch("/admin/api/pool/settings", json={"egress_max_wait": bad})
            self.assertEqual(r.status_code, 400, r.text)
        r = await self.client.patch("/admin/api/pool/settings", json={"egress_rps": 1.5, "egress_burst": 2, "egress_max_wait": 8})
        self.assertEqual(r.status_code, 200, r.text)
        # 保存即热生效
        self.assertEqual(rate_limit.PACER.rps, 1.5)
        self.assertEqual(rate_limit.PACER.burst, 2)
        self.assertEqual(rate_limit.PACER.max_wait, 8.0)
        pool = (await self.client.get("/admin/api/overview")).json()["pool"]
        self.assertEqual(pool["egress_rps"], 1.5)
        # 重启后持久化并重新应用
        await self.client.aclose()
        self.make_app()
        await self.login()
        pool = (await self.client.get("/admin/api/overview")).json()["pool"]
        self.assertEqual(pool["egress_rps"], 1.5)
        self.assertEqual(pool["egress_burst"], 2)
        self.assertEqual(rate_limit.PACER.rps, 1.5)

    async def test_account_proxy_and_cooldown_controls(self):
        await self.login()
        aid = (await self.client.get("/admin/api/overview")).json()["accounts"][0]["id"]
        # 代理校验：非法一律 400
        for bad in ["ftp://host:1", "http://", "http://a b:1", 123, "socks5://", "http://host:70000"]:
            r = await self.client.patch("/admin/api/accounts/" + aid, json={"proxy": bad})
            self.assertEqual(r.status_code, 400, r.text)
        r = await self.client.patch("/admin/api/accounts/" + aid, json={"proxy": "http://127.0.0.1:7897"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((await self.client.get("/admin/api/overview")).json()["accounts"][0]["proxy"], "http://127.0.0.1:7897")
        # VLESS 分享链接也是合法取值（仅解析校验，不会起内核）
        vless_link = "vless://ca997d60-d8db-4eda-8403-1abd8ea8fcc3@h.example:443?type=ws&security=tls&sni=h.example&path=/x"
        self.assertEqual((await self.client.patch("/admin/api/accounts/" + aid, json={"proxy": vless_link})).status_code, 200)
        self.assertEqual((await self.client.patch("/admin/api/accounts/" + aid, json={"proxy": "vless://bad"})).status_code, 400)

        # 手动解除冷却
        self.app.state.pool.record(aid, 429, code=6004)
        self.assertGreater(self.app.state.store.data["account_status"][aid]["cooldown_until"], time.time())
        r = await self.client.post(f"/admin/api/accounts/{aid}/actions/uncool")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.app.state.store.data["account_status"][aid]["cooldown_until"], 0)

        # 永不冷却开关 + 校验
        r = await self.client.patch("/admin/api/accounts/" + aid, json={"never_cooldown": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((await self.client.get("/admin/api/overview")).json()["accounts"][0]["never_cooldown"])
        self.assertEqual((await self.client.patch("/admin/api/accounts/" + aid, json={"never_cooldown": "yes"})).status_code, 400)

    async def test_node_pool_add_probe_assign(self):
        await self.login()
        link = "vless://11111111-2222-3333-4444-555555555555@h.example:443?type=ws&security=tls&sni=h.example&path=/x"
        link2 = link.replace("h.example", "h2.example")
        r = await self.client.post("/admin/api/nodes", json={"text": f"{link}\n{link2}\nnot-a-link\n{link}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "added": 2, "skipped": 2})  # 非法 + 重复
        nodes = (await self.client.get("/admin/api/overview")).json()["nodes"]
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["label"], "h.example:443")
        # 备注 + 禁用第一个节点
        first = nodes[0]["id"]
        r = await self.client.patch("/admin/api/nodes/" + first, json={"remark": "JP1", "enabled": False})
        self.assertEqual(r.status_code, 200, r.text)
        # 自动分配：只剩 1 个启用节点 → 账号代理指向它
        r = await self.client.post("/admin/api/nodes/assign", json={"mode": "round_robin"})
        self.assertEqual(r.status_code, 200, r.text)
        account = (await self.client.get("/admin/api/overview")).json()["accounts"][0]
        self.assertTrue(account["proxy"].startswith("vless://"), account["proxy"])
        # 清除分配
        await self.client.post("/admin/api/nodes/assign", json={"mode": "clear"})
        self.assertEqual((await self.client.get("/admin/api/overview")).json()["accounts"][0]["proxy"], "")
        # 删除
        self.assertEqual((await self.client.delete("/admin/api/nodes/" + first)).status_code, 200)
        self.assertEqual(len((await self.client.get("/admin/api/overview")).json()["nodes"]), 1)

    async def test_stats_endpoint_requires_admin(self):
        self.assertEqual((await self.client.get("/admin/api/stats")).status_code, 401)
        await self.login()
        stats = (await self.client.get("/admin/api/stats")).json()
        for scope in ("today", "all"):
            self.assertIn(scope, stats)
            self.assertEqual(stats[scope]["requests"], 0)
            self.assertEqual(stats[scope]["models"], [])
        self.app.state.metrics.begin()
        self.app.state.metrics.finish("/v1/chat/completions", "api", 200, True, 2500, "success",
                                      usage={"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 75},
                                      ttft_ms=1000, model="deepseek-v4-flash")
        stats = (await self.client.get("/admin/api/stats")).json()
        self.assertEqual(stats["today"]["requests"], 1)
        self.assertEqual(stats["today"]["cache_hit_rate"], 75.0)
        self.assertEqual(stats["today"]["models"][0]["model"], "deepseek-v4-flash")

    async def test_stats_flush_on_shutdown_and_reload(self):
        # Lifespan is what flushes counters, so drive it explicitly: the ASGI
        # transport alone never runs startup/shutdown.
        async with self.app.router.lifespan_context(self.app):
            self.app.state.metrics.begin()
            self.app.state.metrics.finish("/v1/messages", "api", 200, True, 3000, "success",
                                          usage={"input_tokens": 200, "output_tokens": 40, "cache_read_input_tokens": 150},
                                          ttft_ms=1200, model="deepseek-v4-flash")
        self.assertTrue((self.root / "management/mywb.db").exists())
        self.assertFalse((self.root / "management/metrics.json").exists())
        self.make_app()
        await self.login()
        stats = (await self.client.get("/admin/api/stats")).json()
        self.assertEqual(stats["all"]["requests"], 1)
        self.assertEqual(stats["all"]["prompt_tokens"], 200)
        self.assertEqual(stats["all"]["cache_hit_rate"], 75.0)
        self.assertEqual(stats["all"]["days"], 1)

    async def test_cookie_secure_flag_follows_config(self):
        # 默认关闭 Secure，便于纯 HTTP / 局域网访问。
        result = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertNotIn("secure", result.headers["set-cookie"].lower())

        await self.client.aclose()
        self.app.state.db.close()
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN, secure_cookie=True)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://console.test")
        result = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertIn("secure", result.headers["set-cookie"].lower())

    async def test_logout_and_rate_limit(self):
        await self.login()
        self.assertEqual((await self.client.post("/admin/api/logout")).status_code, 200)
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 401)
        for _ in range(8):
            self.assertEqual((await self.client.post("/admin/api/login", json={"key":"wrong"})).status_code, 401)
        self.assertEqual((await self.client.post("/admin/api/login", json={"key":ADMIN})).status_code, 429)

    async def test_session_survives_restart(self):
        await self.login()
        csrf = self.client.headers.get("X-CSRF-Token")
        cookie = self.client.cookies.get("workbuddy_admin")
        # 重启（重建 app 与 client）后，同一 cookie 仍然有效，无需重新登录。
        await self.client.aclose()
        self.make_app()
        self.client.cookies.set("workbuddy_admin", cookie, domain="console.test")
        self.client.headers["X-CSRF-Token"] = csrf
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 200)
        # 登出会删除库内会话，重启后同样失效。
        await self.client.post("/admin/api/logout")
        await self.client.aclose()
        self.make_app()
        self.client.cookies.set("workbuddy_admin", cookie, domain="console.test")
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 401)


if __name__ == "__main__":
    unittest.main()
