"""Account-scoped billing, refresh, daily check-in and request routing."""
import asyncio
import hashlib
import json
import math
import random
import re
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core.site_routing import billing_host_for_headers
from core import rate_limit
from core import vless

REQUEST_CREDENTIAL = ContextVar("pool_credential", default=None)
CN = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 上游错误分层与冷却策略
#
# 目标：把"账号级 / 模型级 / 出口 IP 级"三类问题分开处置，避免一律当成
# "账号 429 冷却 30 分钟"。三条经验来自同类项目实测：
#   1) WAF/限流拦的是**出口 IP**，不是单个账号——账号级冷却在这种场景无效，
#      继续轮转只会加重风控，应当做 IP 级 fail-fast；
#   2) 429 的恢复时间优先读上游给的重置时间，拿不到才用**有界**指数退避；
#   3) 退避要加抖动：按密度判罚的风控会被"齐步走"的固定周期再次聚团。
# ---------------------------------------------------------------------------

# 出口 IP 级 fail-fast：60s 滑窗内"不同账号"命中 WAF 达阈值即判定 IP 被拦。
EGRESS_WINDOW = 60.0
EGRESS_DISTINCT = 2
EGRESS_BLOCK = 300.0

# 各类错误的冷却参数（秒）。
IP_COOLDOWN = 60.0        # WAF/无信封限流：账号侧只做短冷却，主要靠 IP 闸
AUTH_COOLDOWN = 300.0     # 认证被拒
NOT_FOUND_COOLDOWN = 60.0
SOFT_BASE = 60.0          # 账号级软限流退避基数
SOFT_MAX = 1800.0         # 退避封顶
SERVER_COOLDOWN = 120.0   # 连续 5xx 熔断基数
SERVER_MAX = 1800.0
SERVER_THRESHOLD = 3


def classify(status, code=None):
    """按上游返回给账号/出口定性。

    ``status`` 是 HTTP 状态码（或 SSE 错误信封里的数字 code）；
    ``code`` 是业务信封里的 ``code``（没有则为 None）。
    "无业务信封" = 上游根本没回业务结构（例如 WAF 页面）。
    """
    if status is None:
        return "client"
    if status < 400:
        return "ok"
    if status == 402:
        return "credit"
    if status == 401:
        return "auth"
    if status == 403:
        return "auth" if code is not None else "ip"
    if status == 404:
        return "not_found"
    if status == 429:
        # 带业务信封（如 code 6004/14018）是账号/模型级；无信封是出口 IP 级。
        return "rate" if code is not None else "ip"
    if status >= 500 or status in (408, 425):
        return "server"
    return "client"


def _next_daily_reset(now: float) -> float:
    """下一个本地 04:00 的墙钟时间戳（日额度重置后的安全时点）。"""
    current = datetime.fromtimestamp(now, CN)
    reset = current.replace(hour=4, minute=0, second=0, microsecond=0)
    if reset <= current:
        reset += timedelta(days=1)
    return reset.timestamp()


def _envelope_code(text: str):
    """从非 SSE 响应体里尽力提取业务信封 ``code``；无则 None（视为无信封）。"""
    if not text:
        return None
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            code = obj.get("code")
            if isinstance(code, bool):
                return None
            if isinstance(code, int):
                return code
            if isinstance(code, str) and code.lstrip("-").isdigit():
                return int(code)
    match = re.search(r'"code"\s*:\s*"?(-?\d+)', text)
    return int(match.group(1)) if match else None



def number(value):
    if isinstance(value, bool):
        raise ValueError("invalid number")
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid number")
    if not n.is_finite():
        raise ValueError("invalid number")
    return max(Decimal(0), n)


def summarize_packages(packages):
    remain = Decimal(0)
    size = Decimal(0)
    for p in packages:
        # Precise values carry fractional credit balances; cycle values are spendable now.
        prefix = "Cycle" if any(k in p for k in ["CycleCapacityRemainPrecise", "CycleCapacityRemain"]) else ""
        def field(name, default=None):
            return p.get(prefix + name + "Precise", p.get(prefix + name, default))
        r = number(field("CapacityRemain"))
        capacity = number(field("CapacitySize", r))
        remain += min(r, capacity) if capacity else r
        size += capacity
    return {"remaining": float(remain), "capacity": float(size), "used": float(max(Decimal(0), size - remain)), "packages": len(packages)}


class BillingError(Exception):
    def __init__(self, message, status=502, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class AccountPool:
    def __init__(self, store, clock=time.time, client_factory=None):
        self.store, self.clock = store, clock
        self.client_factory = client_factory or (lambda **kw: httpx.Client(timeout=20, follow_redirects=False, **kw))
        self.locks = {}
        self.cursor = 0
        self.inflight = {}
        self.jobs = asyncio.Lock()
        self.last_sync = 0
        # 出口 IP 级 fail-fast 状态（进程维度，不持久化）。
        self._waf_sources = {}          # 窗口内命中 WAF 的账号 -> 最近时间戳
        self._egress_blocked_until = 0.0
        with store.lock:
            store.data.setdefault("pool", {"routing": "round_robin", "auto_checkin": True, "checkin_time": "09:00",
                                           "egress_rps": 2.0, "egress_burst": 1, "egress_max_wait": 10.0})
            store.data.setdefault("account_status", {})
            store.data.setdefault("session_bindings", {})
            store.save()

    def operation_lock(self, aid):
        with self.store.lock:
            return self.locks.setdefault(aid, threading.Lock())

    def snapshot(self, aid):
        with self.store.lock:
            item = self.store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            return dict(item), dict(self.store.data["account_status"].get(aid, {}))

    def update(self, aid, **values):
        with self.store.lock:
            if aid in self.store.data["accounts"]:
                self.store.data["account_status"].setdefault(aid, {}).update(values)
                self.store.save()

    def account_label(self, aid):
        """账号展示名（非凭据），用于请求记录与按账号汇总。"""
        item = self.store.data["accounts"].get(aid) or {}
        name = str(item.get("name") or "").strip()
        return name or aid

    def limit_for(self, aid):
        """单账号并发上限；0 表示不限制。"""
        item = self.store.data["accounts"].get(aid) or {}
        try:
            return max(0, int(item.get("max_concurrent") or 0))
        except (TypeError, ValueError):
            return 0

    def has_capacity(self, aid):
        limit = self.limit_for(aid)
        return limit == 0 or self.inflight.get(aid, 0) < limit

    def release(self, aid):
        """归还一个在途名额（请求结束或重选账号时调用）。"""
        with self.store.lock:
            count = self.inflight.get(aid, 0)
            if count <= 1:
                self.inflight.pop(aid, None)
            else:
                self.inflight[aid] = count - 1

    def state(self, row):
        status = self.store.data["account_status"].get(row["id"], {})
        if not row["enabled"]:
            return "paused"
        if row["status"] == "invalid" or status.get("auth_invalid"):
            return "invalid"
        if not row.get("never_cooldown") and status.get("cooldown_until", 0) > self.clock():
            return "cooling"
        if status.get("credits_updated", 0) > self.clock() - 600 and status.get("remaining") == 0 and status.get("packages", 0) > 0:
            return "exhausted"
        if not self.has_capacity(row["id"]):
            return "busy"
        return "available"

    def rows(self, rows):
        today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
        for row in rows:
            status = self.store.data["account_status"].get(row["id"], {})
            row.update({"pool_state": self.state(row), "remaining": status.get("remaining"), "credits_updated": status.get("credits_updated"),
                        "credits_stale": status.get("credits_updated", 0) < self.clock() - 600,
                        "today_checked_in": status.get("checkin_date") == today, "cooldown_until": status.get("cooldown_until", 0),
                        "last_error": status.get("last_error"), "token_refreshed": status.get("token_refreshed"),
                        "request_count": status.get("request_count", 0),
                        "inflight": self.inflight.get(row["id"], 0), "max_concurrent": self.limit_for(row["id"])})
        return rows

    def select(self, affinity_key=None):
        with self.store.lock:
            blocked = self._egress_blocked_until - self.clock()
            if blocked > 0:
                # IP 级 fail-fast：继续轮转只会加重风控，直接快速失败。
                raise HTTPException(503, f"出口 IP 命中上游风控，已暂停调度 {int(blocked) + 1} 秒")
            candidates = [row for row in self.store.account_rows()
                          if self.state(row) == "available"]
            if self.store.data["pool"]["routing"] == "manual":
                candidates = [row for row in candidates if row["id"] == self.store.data["active"]]
            if not candidates:
                raise HTTPException(503, "暂无可用账号：请检查暂停、积分、冷却、登录状态或并发上限")
            now = self.clock()
            bindings = self.store.data["session_bindings"]
            for key in list(bindings):
                if bindings[key].get("expires", 0) <= now:
                    del bindings[key]
            bound = bindings.get(affinity_key, {}).get("account_id")
            if bound in {row["id"] for row in candidates}:
                aid = bound
            else:
                aid = candidates[self.cursor % len(candidates)]["id"]
                self.cursor += 1
            # 绑定始终跟随本次实际分配的账号：原账号冷却或暂满落到其它账号时，
            # 会话即改绑到新账号，名额释放后不再回到原账号。
            if affinity_key:
                if affinity_key not in bindings and len(bindings) >= 4096:
                    del bindings[min(bindings, key=lambda key: bindings[key]["expires"])]
                bindings[affinity_key] = {"account_id": aid, "expires": now + 86400}
            self.inflight[aid] = self.inflight.get(aid, 0) + 1
            item = self.store.data["accounts"][aid]
            return aid, self.store.manager_for(aid, item)

    def record(self, aid, status=200, code=None):
        """按错误类别处置账号与出口。``code`` 为业务信封 code（无则 None）。"""
        now = self.clock()
        kind = classify(status, code)
        with self.store.lock:
            item = self.store.data["accounts"].get(aid)
            if not item:
                return
            data = self.store.data["account_status"].setdefault(aid, {})
            data["request_count"] = data.get("request_count", 0) + 1
            data["last_request"] = int(now)
            if kind == "ok":
                # 成功是唯一权威的“恢复证据”：清零退避与连败计数。
                data["soft_streak"] = 0
                data["fails"] = 0
                self.store.save()
                return
            if kind == "ip":
                self._note_ip_event(aid, now)
            cooldown, message = self._cooldown_for(kind, data, now)
            if message is not None:
                data["last_error"] = message
            if kind == "auth":
                data["auth_invalid"] = True
            # “永不冷却”账号：只记错误与计数，不写 cooldown_until。
            if cooldown is not None and not item.get("never_cooldown"):
                data["cooldown_until"] = max(data.get("cooldown_until", 0), cooldown) if kind == "server" else cooldown
            self.store.save()

    def _cooldown_for(self, kind, data, now):
        """返回 (冷却截止时间戳或 None, 提示语或 None)；只做状态计算，不落库。"""
        if kind == "ip":
            return now + IP_COOLDOWN, "出口 IP 触发上游 WAF/限流，账号短冷却"
        if kind == "rate":
            streak = int(data.get("soft_streak", 0)) + 1
            data["soft_streak"] = streak
            delay = min(SOFT_BASE * (2 ** (streak - 1)), SOFT_MAX) * random.uniform(0.75, 1.25)
            return now + delay, f"上游频率限制，已退避 {int(delay)} 秒"
        if kind == "credit":
            return _next_daily_reset(now), "积分余额不足，冷却至次日 04:00"
        if kind == "auth":
            return now + AUTH_COOLDOWN, "上游认证或访问被拒绝，已冷却"
        if kind == "server":
            fails = int(data.get("fails", 0)) + 1
            data["fails"] = fails
            if fails >= SERVER_THRESHOLD:
                delay = min(SERVER_COOLDOWN * (2 ** (fails - SERVER_THRESHOLD)), SERVER_MAX)
                return now + delay, "上游连续服务端错误，已熔断"
            return None, f"上游服务端错误（{fails}/{SERVER_THRESHOLD}）"
        if kind == "not_found":
            return now + NOT_FOUND_COOLDOWN, "上游返回 404，已短冷却"
        return None, None  # client：不处罚账号

    def uncool(self, aid):
        """手动解除账号冷却（同时清零退避与连败计数）。"""
        with self.store.lock:
            if aid not in self.store.data["accounts"]:
                raise HTTPException(404, "账号不存在")
            data = self.store.data["account_status"].setdefault(aid, {})
            data["cooldown_until"] = 0
            data["soft_streak"] = 0
            data["fails"] = 0
            data["last_error"] = None
            self.store.save()
        return {"id": aid, "ok": True, "message": "已解除冷却"}

    def _note_ip_event(self, aid, now):
        """记录一次出口 IP 级命中；60s 窗口内不同账号达阈值即打开 IP 闸。"""
        self._waf_sources = {k: t for k, t in self._waf_sources.items() if t > now - EGRESS_WINDOW}
        self._waf_sources[aid] = now
        if len(self._waf_sources) >= EGRESS_DISTINCT and now >= self._egress_blocked_until:
            self._egress_blocked_until = now + EGRESS_BLOCK

    def egress_blocked_seconds(self):
        """出口 IP 闸剩余秒数（0 = 未拦）。"""
        return max(0, int(math.ceil(self._egress_blocked_until - self.clock())))

    def billing(self, client, headers, path, body=None):
        host = billing_host_for_headers(headers)
        # 签到 / 余额 / 积分等短 RPC 也走同一个出口，纳入匀速器避免叠加突发。
        rate_limit.PACER.acquire()
        response = client.post(host + "/v2/billing/meter/" + path, headers=headers, json=body or {})
        if response.status_code != 200:
            raise BillingError("积分服务请求失败", response.status_code)
        try:
            d = response.json()
        except ValueError:
            raise BillingError("积分服务返回格式异常")
        if not isinstance(d, dict) or d.get("code") != 0:
            raise BillingError("上游未接受该操作，请检查账号或稍后重试", code=d.get("code") if isinstance(d, dict) else None)
        return d.get("data")

    def credits(self, client, headers):
        packages = []
        now = datetime.fromtimestamp(self.clock(), CN)
        for page in range(1, 101):
            d = self.billing(client, headers, "get-user-resource", {"PageNumber": page, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3], "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"), "PackageEndTimeRangeEnd": "2126-01-01 00:00:00"})
            try:
                data = d["Response"]["Data"]
                current = data["Accounts"]
                total = int(data["TotalCount"])
                if not isinstance(current, list) or total < 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise BillingError("积分数据格式异常，上次余额已保留")
            packages.extend(current)
            if len(packages) >= total:
                return summarize_packages(packages)
            if not current:
                break
        raise BillingError("积分包分页不完整，上次余额已保留")

    def checkin_status(self, client, headers):
        try:
            d = self.billing(client, headers, "checkin-activity-status")
        except BillingError as e:
            if e.status not in (404, 405):
                raise
            d = self.billing(client, headers, "checkin-status")
        if not isinstance(d, dict):
            raise BillingError("签到状态格式异常")
        def flag(snake, camel):
            v = d.get(snake, d.get(camel))
            if v not in (True, False, 0, 1, "true", "false", "1", "0"):
                raise BillingError("签到状态缺少必要字段")
            return v in (True, 1, "true", "1")
        return {"active": flag("active", "active"), "checked": flag("today_checked_in", "todayCheckedIn")}

    def operate(self, aid, action):
        with self.operation_lock(aid):
            item, previous = self.snapshot(aid)
            if not item["enabled"]:
                return {"id": aid, "ok": False, "message": "账号已暂停，未执行操作"}
            manager = self.store.manager_for(aid, item)
            try:
                if action == "refresh":
                    with manager._lock:
                        manager._refresh()
                    self.update(aid, token_refreshed=int(self.clock()), auth_invalid=False, cooldown_until=0, last_error=None)
                    return {"id": aid, "ok": True, "message": "登录凭据已刷新"}
                headers = manager.get_headers(purpose="billing")
                today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
                with self.client_factory(proxy=vless.resolve(getattr(manager, "proxy", None))) as client:
                    message = "积分状态已更新"
                    checkin_error = None
                    try:
                        ci = self.checkin_status(client, headers)
                        if ci["checked"]:
                            self.update(aid, checkin_date=today)
                        if action == "checkin":
                            if ci["checked"]:
                                message = "今日已签到，无需重复签到"
                            elif not ci["active"]:
                                message = "当前没有可参与的签到活动"
                            else:
                                self.billing(client, headers, "daily-checkin")
                                ci = self.checkin_status(client, headers)
                                if not ci["checked"]:
                                    raise BillingError("签到已提交，但上游尚未确认；请刷新状态")
                                self.update(aid, checkin_date=today)
                                message = "签到成功"
                    except (BillingError, httpx.HTTPError) as e:
                        if action == "checkin":
                            raise
                        checkin_error = "签到状态暂时无法查询"
                    data = self.credits(client, headers)
                    # A successful balance lookup is proof of current authentication.
                    self.update(aid, **data, credits_updated=int(self.clock()), auth_invalid=False, last_error=checkin_error)
                    if data["remaining"] > 0 and action == "checkin":
                        self.update(aid, cooldown_until=0)
                    return {"id": aid, "ok": True, "message": message, "remaining": data["remaining"]}
            except BillingError as e:
                self.update(aid, last_error=str(e))
                if e.status in (401, 403):
                    self.update(aid, cooldown_until=self.clock()+300)
                return {"id": aid, "ok": False, "message": str(e)}
            except (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError):
                # Do not expose raw refresh responses or bearer credentials.
                message = "操作失败：请检查登录凭据或网络，必要时重新授权"
                self.update(aid, last_error=message)
                return {"id": aid, "ok": False, "message": message}

    async def batch(self, action):
        if self.jobs.locked():
            raise HTTPException(409, "已有批量任务进行中，请稍后刷新")
        async with self.jobs:
            with self.store.lock:
                ids = [aid for aid, item in self.store.data["accounts"].items() if item["enabled"]]
            semaphore = asyncio.Semaphore(3)
            async def run(aid):
                async with semaphore:
                    try:
                        return await asyncio.to_thread(self.operate, aid, action)
                    except HTTPException:
                        return {"id": aid, "ok": False, "message": "账号已移除"}
            return {"results": await asyncio.gather(*(run(aid) for aid in ids))}

    async def tick(self):
        now = datetime.fromtimestamp(self.clock(), CN)
        with self.store.lock:
            cfg = dict(self.store.data["pool"])
            due = [aid for aid, item in self.store.data["accounts"].items() if item["enabled"] and self.store.data["account_status"].get(aid, {}).get("checkin_date") != now.strftime("%Y-%m-%d") and self.store.data["account_status"].get(aid, {}).get("auto_attempt", 0) < self.clock()-1800]
        if cfg["auto_checkin"] and now.strftime("%H:%M") >= cfg["checkin_time"]:
            for aid in due:
                self.update(aid, auto_attempt=int(self.clock()))
                try:
                    await asyncio.to_thread(self.operate, aid, "checkin")
                except HTTPException:
                    pass
        if self.clock() - self.last_sync > 300 and not self.jobs.locked():
            await self.batch("status")
            self.last_sync = self.clock()


def request_affinity(headers, body):
    """Persist only a hash of caller/model/session identity, never raw prompts or keys."""
    if not isinstance(body, dict):
        return None
    identity = None
    for name in (b"x-session-id", b"session_id", b"x-conversation-id"):
        if headers.get(name):
            identity = [name.decode(), headers[name].decode(errors="replace")]
            break
    if identity is None:
        for name in ("prompt_cache_key", "conversation_id", "session_id"):
            if body.get(name):
                identity = [name, body[name]]
                break
    if identity is None:
        messages = body.get("messages", body.get("input", []))
        if isinstance(messages, str) and messages:
            identity = ["first_user", messages]
        elif isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "user":
                    identity = ["first_user", message.get("content", "")]
                    break
    if identity is None:
        return None
    caller = headers.get(b"authorization") or headers.get(b"x-api-key", b"")
    value = [caller.decode(errors="replace"), body.get("model", "auto"), identity]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PoolMiddleware:
    def __init__(self, app, pool):
        self.app, self.pool = app, pool

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        aid = None
        try:
            self.pool.store.check_api(headers.get(b"authorization", b"").decode(), headers.get(b"x-api-key", b"").decode())
            raw = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                raw.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            raw = bytes(raw)
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            affinity_key = request_affinity(headers, body)
            original_receive = receive
            delivered = False
            async def replay_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await original_receive()
            receive = replay_receive
            aid, manager = self.pool.select(affinity_key)
            # Refresh before entering a stream; retry another eligible account only
            # if credential preparation fails, never replay a partially emitted response.
            try:
                await asyncio.to_thread(manager.get_headers)
            except Exception:
                self.pool.update(aid, cooldown_until=self.pool.clock()+300, last_error="凭据暂不可用，已冷却 5 分钟")
                self.pool.release(aid)
                aid = None
                aid, manager = self.pool.select(affinity_key)
                await asyncio.to_thread(manager.get_headers)
        except HTTPException as e:
            if aid is not None:
                self.pool.release(aid)
            return await JSONResponse({"detail": e.detail}, e.status_code)(scope, receive, send)
        except Exception:
            if aid is not None:
                self.pool.release(aid)
            return await JSONResponse({"detail": "账号凭据暂不可用，请刷新凭据或重新授权"}, 503)(scope, receive, send)
        token = REQUEST_CREDENTIAL.set(manager)
        # 记录本次实际选中的账号展示名，供外层 MetricsMiddleware 按账号汇总。
        scope["account_label"] = self.pool.account_label(aid)
        status = 200
        envelope = None
        sse_buffer = b""
        probe = bytearray()

        def absorb(chunk):
            if chunk and len(probe) < 65536:
                probe.extend(chunk[: 65536 - len(probe)])

        async def observe(message):
            nonlocal status, envelope, sse_buffer
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                absorb(chunk)
                sse_buffer += chunk
                while b"\n" in sse_buffer:
                    line, sse_buffer = sse_buffer.split(b"\n", 1)
                    if not line.startswith(b"data:"):
                        continue
                    try:
                        obj = json.loads(line[5:])
                    except ValueError:
                        continue
                    err = obj.get("error") or (obj.get("response") or {}).get("error") or {}
                    code = err.get("code") if isinstance(err, dict) else None
                    if isinstance(code, str) and code.isdigit():
                        code = int(code)
                    if isinstance(code, int):
                        envelope = code
                        # SSE 错误信封里的数字常见为 HTTP 语义码（如 429）。
                        if code in (401, 402, 403, 429):
                            status = code
                if len(sse_buffer) > 65536:
                    sse_buffer = b""
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            REQUEST_CREDENTIAL.reset(token)
            self.pool.release(aid)
            if envelope is None:
                envelope = _envelope_code(bytes(probe).decode("utf-8", "replace"))
            self.pool.record(aid, status, envelope)
