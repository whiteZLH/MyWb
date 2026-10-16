"""按凭据身份选择产品/地区端点，绝不接受任意输入 URL。

四象限 profile：{cn,intl} × {cli,work}
  cn-cli   -> copilot.tencent.com   （国内 CodeBuddy 桌面端 / CLI）
  cn-work  -> www.workbuddy.cn      （国内 WorkBuddy）
  intl-cli -> www.codebuddy.ai      （国际 CodeBuddy）
  intl-work-> www.workbuddy.ai      （国际 WorkBuddy AI）

凭据的 domain 字段与 accessToken 的 JWT iss 声明都只作为路由提示，
上游会自行校验签名与账号权限。
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid

DOMESTIC = "domestic"
INTERNATIONAL = "international"

PROFILE_ENDPOINTS = {
    "cn-cli": "https://copilot.tencent.com",
    "cn-work": "https://www.workbuddy.cn",
    "intl-cli": "https://www.codebuddy.ai",
    "intl-work": "https://www.workbuddy.ai",
}

# 计费/签到走产品网站，而不是 CLI chat 端点。
BILLING_PROFILE_HOSTS = {
    "cn-cli": "https://www.codebuddy.cn",
    "cn-work": "https://www.workbuddy.cn",
    "intl-cli": "https://www.codebuddy.ai",
    "intl-work": "https://www.workbuddy.ai",
}

DOMAIN_PROFILES = {
    "www.codebuddy.cn": "cn-cli",
    "www.workbuddy.cn": "cn-work",
    "copilot.tencent.com": "cn-cli",
    "www.codebuddy.ai": "intl-cli",
    "www.workbuddy.ai": "intl-work",
}

DEFAULT_PROFILE = "cn-cli"
DEFAULT_DOMAIN = "www.codebuddy.cn"

# 已验证的产品版本号：上游 /v3/config 会校验 UA 中的产品版本。
CLI_VERSION = "2.149.0"
WORKBUDDY_VERSION = "5.5.2"
WORKBUDDY_CLI_VERSION = "2.137.1"

_REFRESH_PATH = "/v2/plugin/auth/token/refresh"
_CHAT_PATH = "/v2/chat/completions"


def profile_region(profile: str) -> str:
    if profile not in PROFILE_ENDPOINTS:
        raise ValueError("unknown credential profile")
    return profile.split("-", 1)[0]


def profile_product(profile: str) -> str:
    profile_region(profile)
    return "workbuddy" if profile.endswith("-work") else "cli"


def profile_site(profile: str) -> str:
    return INTERNATIONAL if profile_region(profile) == "intl" else DOMESTIC


def site_for_profile(profile: str) -> str:
    """按 profile 返回地域标识。"""
    return profile_site(profile)


def identity_headers(profile: str) -> dict:
    """按产品线返回身份头；上游会据此选择模型目录。"""
    if profile_product(profile) == "cli":
        return {
            "User-Agent": f"CLI/{CLI_VERSION} CodeBuddy/{CLI_VERSION}",
            "X-IDE-Type": "CLI",
            "X-IDE-Name": "CLI",
            "X-IDE-Version": CLI_VERSION,
        }
    name = "WorkBuddy AI" if profile_region(profile) == "intl" else "WorkBuddy"
    return {
        "User-Agent": (
            f"WorkBuddy/{WORKBUDDY_VERSION} {name}/{WORKBUDDY_VERSION}"
            f" CLI/{WORKBUDDY_CLI_VERSION}"
        ),
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": WORKBUDDY_VERSION,
    }


def conversation_headers() -> dict:
    """官方客户端的会话头族；上游渠道校验缺它时回 400 / code 11-128。

    自 2026-10-16 起，国际站（www.workbuddy.ai）的 /v2/chat/completions 渠道
    校验要求请求带 X-Conversation-ID / -Request-ID / -Message-ID，缺任一项即
    返回 HTTP 400、code 11-128、msg「conversation id missing」。上游不按这个
    id 保存历史，它只是渠道/会话标记（实测同一 id 连发两轮仍无上下文），所以
    每次请求现造一组互不相同的 v4 UUID 即可，无需维护会话映射。国内站带与不带
    都通过，故两站统一发送，避免两条路径分叉。
    """
    return {
        "X-Conversation-ID": str(uuid.uuid4()),
        "X-Conversation-Request-ID": str(uuid.uuid4()),
        "X-Conversation-Message-ID": str(uuid.uuid4()),
    }


def _token_issuer_host(token) -> str | None:
    """解出 JWT iss 的主机名；失败返回 None。"""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    try:
        claims = json.loads(
            base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        return None
    if not isinstance(claims, dict):
        return None
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer:
        return None
    host = issuer.split("//", 1)[-1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return host.split(":", 1)[0].lower() or None


def _hint_hosts(auth: dict) -> list[str]:
    """从凭据里提取已知域名提示，忽略未知或畸形值。"""
    hosts = []
    for value in (auth.get("domain"), auth.get("issuer"), _token_issuer_host(auth.get("accessToken"))):
        if not isinstance(value, str) or not value:
            continue
        host = value.strip().lower()
        if "//" in host:
            host = host.split("//", 1)[1]
        host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split(":", 1)[0]
        if host in DOMAIN_PROFILES and host not in hosts:
            hosts.append(host)
    return hosts


def _resolve_profile(auth: dict) -> str:
    hosts = _hint_hosts(auth)
    # copilot.tencent.com 是国内 CLI 与 WorkBuddy 共用的端点，无法区分产品线。
    branded = [host for host in hosts if host != "copilot.tencent.com"]
    if not branded:
        return DEFAULT_PROFILE
    profiles = {DOMAIN_PROFILES[host] for host in branded}
    if len(profiles) > 1:
        raise ValueError("conflicting credential products")
    return branded and next(iter(profiles)) or DEFAULT_PROFILE


def profile_for_auth(auth) -> str:
    """从凭据推断 profile；无法判断时回落到国内 CLI。"""
    if not isinstance(auth, dict):
        return DEFAULT_PROFILE
    return _resolve_profile(auth)


def domain_for_auth(auth: dict) -> str:
    """返回凭据的品牌域名，用于 X-Domain。

    优先取凭据自身的 domain（沿用原有行为），其次 JWT 签发方；
    两者都不可用时回落国内默认域名。取值经过白名单校验，不接受任意输入。
    """
    hosts = _hint_hosts(auth)
    return hosts[0] if hosts else DEFAULT_DOMAIN


def profile_for_headers(headers) -> str:
    """从已构造的后端请求头里反查 profile（管理后台按账号选择端点用）。"""
    if not headers:
        return DEFAULT_PROFILE
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return profile_for_auth(
        {"domain": lowered.get("x-domain"), "accessToken": _bearer(lowered.get("authorization"))}
    )


def _bearer(value):
    if not isinstance(value, str):
        return None
    parts = value.split(None, 1)
    return parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else None


def refresh_url_for_auth(auth: dict) -> str:
    return PROFILE_ENDPOINTS[profile_for_auth(auth)] + _REFRESH_PATH


def chat_url_for_auth(auth: dict) -> str:
    return PROFILE_ENDPOINTS[profile_for_auth(auth)] + _CHAT_PATH


def chat_url_for_headers(headers) -> str:
    return PROFILE_ENDPOINTS[profile_for_headers(headers)] + _CHAT_PATH


def billing_host_for_auth(auth: dict) -> str:
    return BILLING_PROFILE_HOSTS[profile_for_auth(auth)]


def billing_host_for_headers(headers) -> str:
    return BILLING_PROFILE_HOSTS[profile_for_headers(headers)]
