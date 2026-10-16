import base64
import json
import re
import unittest
from unittest.mock import Mock

import httpx


def jwt(iss):
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{seg({'alg': 'RS256'})}.{seg({'iss': iss})}.sig"

from admin import browser_login
from admin.browser_login import BrowserLogin, SITE_HOSTS, SITE_PROFILES, base_headers
import core.converter as converter
from core.site_routing import (
    BILLING_PROFILE_HOSTS,
    DEFAULT_DOMAIN,
    PROFILE_ENDPOINTS,
    billing_host_for_headers,
    chat_url_for_auth,
    chat_url_for_headers,
    conversation_headers,
    domain_for_auth,
    identity_headers,
    profile_for_auth,
    refresh_url_for_auth,
)

CN_CLI = {"accessToken": "t", "domain": "www.codebuddy.cn"}
CN_WORK = {"accessToken": "t", "domain": "www.workbuddy.cn"}
INTL_CLI = {"accessToken": "t", "domain": "www.codebuddy.ai"}
INTL_WORK = {"accessToken": "t", "domain": "www.workbuddy.ai"}


class ProfileRoutingTests(unittest.TestCase):
    def test_domain_selects_quadrant(self):
        self.assertEqual(profile_for_auth(CN_CLI), "cn-cli")
        self.assertEqual(profile_for_auth(CN_WORK), "cn-work")
        self.assertEqual(profile_for_auth(INTL_CLI), "intl-cli")
        self.assertEqual(profile_for_auth(INTL_WORK), "intl-work")

    def test_shared_tencent_endpoint_alone_cannot_pick_a_product(self):
        # copilot.tencent.com 是国内 CLI 与 WorkBuddy 共用的插件端点。
        self.assertEqual(profile_for_auth({"domain": "copilot.tencent.com"}), "cn-cli")

    def test_domain_preserves_credential_value_and_rejects_unknown(self):
        # X-Domain 沿用凭据原值（含共享端点），未知域名不被透传。
        self.assertEqual(domain_for_auth({"domain": "copilot.tencent.com"}), "copilot.tencent.com")
        self.assertEqual(domain_for_auth({"domain": "www.workbuddy.ai"}), "www.workbuddy.ai")
        self.assertEqual(domain_for_auth({"domain": "evil.example"}), DEFAULT_DOMAIN)
        self.assertEqual(domain_for_auth({}), DEFAULT_DOMAIN)

    def test_domain_falls_back_to_jwt_issuer(self):
        self.assertEqual(domain_for_auth({"accessToken": jwt("https://www.workbuddy.ai/auth/realms/copilot")}), "www.workbuddy.ai")
        self.assertEqual(domain_for_auth({"accessToken": "not-a-jwt"}), DEFAULT_DOMAIN)

    def test_unknown_or_missing_domain_falls_back_to_domestic_cli(self):
        for auth in ({}, {"domain": ""}, {"domain": "evil.example"}, None, {"domain": 42}):
            self.assertEqual(profile_for_auth(auth), "cn-cli")

    def test_conflicting_hints_are_rejected(self):
        # domain 指向国内 WorkBuddy，JWT iss 指向国际 WorkBuddy -> 站点冲突。
        with self.assertRaises(ValueError):
            profile_for_auth({"domain": "www.workbuddy.cn", "accessToken": jwt("https://www.workbuddy.ai/auth/realms/copilot")})

    def test_token_issuer_is_used_when_domain_is_absent(self):
        self.assertEqual(profile_for_auth({"accessToken": jwt("https://www.workbuddy.ai/auth/realms/copilot")}), "intl-work")
        self.assertEqual(profile_for_auth({"accessToken": jwt("https://copilot.tencent.com/auth/realms/copilot")}), "cn-cli")
        # 畸形 JWT 不参与判断，回落到默认站点。
        self.assertEqual(profile_for_auth({"accessToken": "not-a-jwt"}), "cn-cli")

    def test_conflicting_credential_is_reported_as_400_not_500(self):
        manager = converter.CredentialManager.__new__(converter.CredentialManager)
        with self.assertRaises(converter.HTTPException) as ctx:
            manager._build_headers_from(
                {"domain": "www.workbuddy.cn", "accessToken": jwt("https://www.workbuddy.ai/auth/realms/copilot")}, {}
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_endpoints_follow_profile(self):
        self.assertEqual(chat_url_for_auth(INTL_WORK), "https://www.workbuddy.ai/v2/chat/completions")
        self.assertEqual(chat_url_for_auth(CN_CLI), "https://copilot.tencent.com/v2/chat/completions")
        self.assertEqual(refresh_url_for_auth(INTL_CLI), "https://www.codebuddy.ai/v2/plugin/auth/token/refresh")
        self.assertEqual(BILLING_PROFILE_HOSTS["cn-cli"], "https://www.codebuddy.cn")
        self.assertEqual(BILLING_PROFILE_HOSTS["intl-work"], "https://www.workbuddy.ai")

    def test_identity_headers_differ_between_products(self):
        cli = identity_headers("intl-cli")
        work = identity_headers("intl-work")
        self.assertEqual(cli["X-IDE-Type"], "CLI")
        self.assertEqual(work["X-IDE-Type"], "WorkBuddy")
        # 上游 /v3/config 会校验 UA 中的产品版本。
        self.assertIn("CLI/", cli["User-Agent"])
        self.assertIn("WorkBuddy/", work["User-Agent"])
        self.assertIn("WorkBuddy AI", work["User-Agent"])

    def test_billing_host_from_headers_matches_domain(self):
        for auth, expected in ((CN_CLI, "https://www.codebuddy.cn"),
                               (CN_WORK, "https://www.workbuddy.cn"),
                               (INTL_CLI, "https://www.codebuddy.ai"),
                               (INTL_WORK, "https://www.workbuddy.ai")):
            headers = converter.CredentialManager.__new__(converter.CredentialManager)._build_headers_from(auth, {})
            self.assertEqual(billing_host_for_headers(headers), expected)
            self.assertEqual(chat_url_for_headers(headers), PROFILE_ENDPOINTS[profile_for_auth(auth)] + "/v2/chat/completions")

    def test_default_models_are_region_specific(self):
        self.assertIn("glm-5.2", converter.default_models_for_profile("cn-cli"))
        self.assertNotIn("gpt-5.6-luna", converter.default_models_for_profile("cn-cli"))
        self.assertIn("gpt-5.6-luna", converter.default_models_for_profile("intl-work"))
        self.assertNotIn("glm-5v-turbo", converter.default_models_for_profile("intl-work"))


CONV_TRIO = ("X-Conversation-ID", "X-Conversation-Request-ID", "X-Conversation-Message-ID")
UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class ConversationHeaderTests(unittest.TestCase):
    """上游渠道校验要求 chat 请求带会话头族，缺任一项回 400 / code 11-128。"""

    @staticmethod
    def headers(auth, purpose="chat"):
        return converter.CredentialManager.__new__(converter.CredentialManager)._build_headers_from(
            auth, {}, purpose=purpose
        )

    def test_conversation_headers_are_three_distinct_v4_uuids(self):
        headers = conversation_headers()
        values = [headers[name] for name in CONV_TRIO]
        for value in values:
            self.assertRegex(value, UUID_V4)
        # 三个值必须互不相同：复用同一 id 会被上游判定为非法。
        self.assertEqual(len(set(values)), 3)

    def test_each_call_mints_a_fresh_trio(self):
        first = conversation_headers()
        second = conversation_headers()
        for name in CONV_TRIO:
            self.assertNotEqual(first[name], second[name])

    def test_chat_headers_carry_the_conversation_trio(self):
        for auth in (CN_CLI, CN_WORK, INTL_CLI, INTL_WORK):
            headers = self.headers(auth)
            for name in CONV_TRIO:
                self.assertRegex(headers[name], UUID_V4)
            values = [headers[name] for name in CONV_TRIO]
            self.assertEqual(len(set(values)), 3)

    def test_refresh_catalog_and_billing_omit_the_conversation_trio(self):
        for purpose in ("refresh", "catalog", "billing"):
            headers = self.headers(INTL_WORK, purpose=purpose)
            for name in CONV_TRIO:
                self.assertNotIn(name, headers)


class LeadingSystemTests(unittest.TestCase):
    def test_intl_inserts_system_when_missing(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        out = converter.ensure_leading_system(body, "intl-work")
        self.assertEqual(out["messages"][0]["role"], "system")
        self.assertEqual(len(out["messages"]), 2)
        # 原 body 不能被就地修改
        self.assertEqual(len(body["messages"]), 1)

    def test_intl_keeps_existing_leading_system(self):
        body = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]}
        self.assertIs(converter.ensure_leading_system(body, "intl-cli"), body)

    def test_domestic_is_untouched(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertIs(converter.ensure_leading_system(body, "cn-cli"), body)

    def test_empty_message_list_is_untouched(self):
        body = {"messages": []}
        self.assertIs(converter.ensure_leading_system(body, "intl-work"), body)


class BrowserLoginSitesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.time = 10000
        self.authorized = False
        self.credential_domain = None
        self.saver = Mock(return_value={"account_id": "saved-id", "updated": False})
        self.calls = []

        def respond(req):
            self.calls.append(req)
            if req.url.path.endswith('/state'):
                return httpx.Response(200, json={"code": 0, "data": {"state": "st", "authUrl": f"https://{req.url.host}/login?state=st"}})
            if req.url.path.endswith('/token'):
                if not self.authorized:
                    return httpx.Response(200, json={"code": 11217})
                # 默认按站点主机返回凭据；测试可强制指定成其它站点以模拟不匹配。
                domain = self.credential_domain or req.url.host
                return httpx.Response(200, json={"code": 0, "data": {"accessToken": "a", "refreshToken": "r", "expiresIn": 3600, "domain": domain}})
            return httpx.Response(200, json={"code": 0, "data": {"uid": "u1", "nickname": "n"}})

        self.manager = BrowserLogin(self.saver, lambda site: httpx.AsyncClient(transport=httpx.MockTransport(respond)), lambda: self.time)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_intl_site_authorizes_international_account(self):
        flow = await self.manager.start('o', None, site="intl")
        self.assertTrue(flow["url"].startswith("https://www.workbuddy.ai/login"))
        self.authorized = True
        self.time += 4
        result = await self.manager.poll(flow["id"], 'o')
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.saver.call_args.args[0]["auth"]["domain"], "www.workbuddy.ai")

    async def test_rejects_credential_from_a_different_site(self):
        # 选择国际 WorkBuddy 站点，但上游返回国内凭据 -> 必须拒绝写入。
        self.authorized = True
        self.credential_domain = "www.codebuddy.cn"
        flow = await self.manager.start('o', None, site="intl")
        self.time += 4
        with self.assertRaises(Exception) as ctx:
            await self.manager.poll(flow["id"], 'o')
        self.assertEqual(ctx.exception.status_code, 400)
        self.saver.assert_not_called()

    async def test_matching_credential_is_saved(self):
        self.authorized = True
        self.credential_domain = "www.workbuddy.ai"
        flow = await self.manager.start('o', None, site="intl")
        self.time += 4
        result = await self.manager.poll(flow["id"], 'o')
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.saver.call_args.args[0]["auth"]["domain"], "www.workbuddy.ai")

    async def test_unknown_site_is_rejected(self):
        with self.assertRaises(Exception) as ctx:
            await self.manager.start('o', None, site="mars")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_each_site_uses_its_own_origin_and_host(self):
        for site, host in SITE_HOSTS.items():
            headers = base_headers(site)
            origin = headers["Origin"]
            self.assertTrue(origin.startswith("https://"))
            self.assertEqual(headers["Referer"], origin + "/")
            flow = await self.manager.start('o', None, site=site)
            self.assertTrue(flow["url"].startswith(host + "/login"))
            await self.manager.cancel(flow["id"], 'o')
        # 国际 CodeBuddy 需要大写 CLI，其余站点用 workbuddy。
        self.assertEqual(browser_login.base_headers("cn")["X-IDE-Type"], "CLI")
        self.assertEqual(browser_login.base_headers("intl")["X-IDE-Type"], "WorkBuddy")
        self.assertEqual(browser_login.SITE_PROFILES["intl"], "intl-work")
        self.assertEqual(browser_login.SITE_PROFILES["intl-codebuddy"], "intl-cli")


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        converter._catalog_cache.clear()

    def tearDown(self):
        converter._catalog_cache.clear()

    def test_upstream_catalog_is_cached_per_profile(self):
        calls = []

        def fake_get(self, url, headers=None):
            calls.append(url)
            return httpx.Response(200, json={"data": {"models": [{"id": "m-a"}, {"id": "m-a"}, {"id": "m-b"}]}}, request=httpx.Request("GET", url))

        original = httpx.Client.get
        httpx.Client.get = fake_get
        try:
            first = converter._fetch_models_from_upstream("intl-work", {})
            second = converter._fetch_models_from_upstream("intl-work", {})
        finally:
            httpx.Client.get = original
        # 去重且命中缓存，只打一次上游。
        self.assertEqual(first, ["m-a", "m-b"])
        self.assertEqual(second, ["m-a", "m-b"])
        self.assertEqual(len(calls), 1)

    def test_force_bypasses_cache(self):
        calls = []

        def fake_get(self, url, headers=None):
            calls.append(url)
            return httpx.Response(200, json={"data": {"models": [{"id": "m-a"}]}}, request=httpx.Request("GET", url))

        original = httpx.Client.get
        httpx.Client.get = fake_get
        try:
            converter._fetch_models_from_upstream("intl-work", {})
            converter._fetch_models_from_upstream("intl-work", {}, force=True)
        finally:
            httpx.Client.get = original
        self.assertEqual(len(calls), 2)

    def test_failure_is_not_cached(self):
        calls = []

        def fake_get(self, url, headers=None):
            calls.append(url)
            return httpx.Response(500, json={}, request=httpx.Request("GET", url))

        original = httpx.Client.get
        httpx.Client.get = fake_get
        try:
            self.assertEqual(converter._fetch_models_from_upstream("intl-work", {}), [])
            converter._fetch_models_from_upstream("intl-work", {})
        finally:
            httpx.Client.get = original
        # 失败不写缓存，允许重试。
        self.assertEqual(len(calls), 2)

    def test_non_chat_models_are_filtered(self):
        def fake_get(self, url, headers=None):
            return httpx.Response(200, json={"data": {"models": [
                {"id": "chat"},
                {"id": "img", "tags": ["text-to-image"]},
                {"id": "vid", "tags": ["text-to-video"]},
            ]}}, request=httpx.Request("GET", url))

        original = httpx.Client.get
        httpx.Client.get = fake_get
        try:
            ids = converter._fetch_models_from_upstream("intl-work", {})
        finally:
            httpx.Client.get = original
        self.assertEqual(ids, ["chat"])


if __name__ == "__main__":
    unittest.main()
