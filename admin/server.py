"""Small, persistent management layer for MyWb. Single-process deployment."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from core import converter
from core import settings
from core import site_routing
from .browser_login import DEFAULT_SITE, SITE_HOSTS, BrowserLogin
from .db import Database
from .pool import AccountPool, PoolMiddleware
from .metrics import RequestMetrics, MetricsMiddleware

COOKIE = "workbuddy_admin"
MAX_BODY = 1024 * 1024
ASSETS = Path(__file__).parent / "static"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def clean_name(value, fallback):
    if value is None:
        return fallback
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60:
        raise HTTPException(400, "名称需为 1–60 个字符")
    return value.strip()


class Store:
    def __init__(self, root, auth_dir, initial_key, admin_key, db=None):
        if len(admin_key) < 20:
            raise RuntimeError("ADMIN_KEY must contain at least 20 characters")
        self.root, self.auth_dir = Path(root), Path(auth_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.admin_digest = digest(admin_key)
        self.attempts = {}
        self.test_lock = asyncio.Lock()
        self.test_keys = set()
        self.managers = {}
        self.started = time.time()
        self.events = deque(maxlen=40)
        # 数据库是唯一数据源；`self.data` 保留为内存快照，兼容既有调用点与测试。
        self.db = db if db is not None else Database(self.root / "mywb.db")
        self.path = self.db.path
        self._migrate_legacy_json()
        if self.db.get_meta("initialized") != "1":
            self._bootstrap(initial_key)
        self.data = self.db.load_state()
        self.sessions = self.db.sessions()
        self.sync_active()

    def _bootstrap(self, initial_key):
        """首次运行：从 auth/*.info 建立账号索引，并导入初始 API Key。"""
        data = {"accounts": {}, "active": None, "keys": {}}
        for p in sorted(self.auth_dir.glob("*.info")):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                self.validate_credential(doc)
            except (ValueError, OSError, HTTPException):
                continue
            aid = secrets.token_hex(8)
            name = str(doc.get("account", {}).get("nickname") or "已迁移账号")[:60]
            data["accounts"][aid] = {"name": name, "file": p.name, "enabled": not bool(doc.get("disabled")), "created": int(time.time())}
            if data["active"] is None and not doc.get("disabled"):
                data["active"] = aid
        if initial_key:
            value = initial_key
            data["keys"][secrets.token_hex(8)] = {"name": "原有 API Key", "hash": digest(value), "hint": value[:5] + "…" + value[-4:], "created": int(time.time())}
        self.db.save_state(data)
        self.db.set_meta("initialized", 1)

    def _migrate_legacy_json(self):
        """把旧 state.json 导入数据库，成功后删除原文件。

        metrics.json 的迁移由 ``RequestMetrics`` 负责（它掌握桶结构）。
        """
        state_path = self.root / "state.json"
        if not state_path.exists():
            return
        if self.db.get_meta("initialized") == "1":
            # 数据库已在用，遗留 JSON 视为陈旧备份，直接清理。
            self._discard_legacy(state_path)
            return
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("accounts", {})
                data.setdefault("keys", {})
                self.db.save_state(data)
                self.db.set_meta("initialized", 1)
        except (ValueError, OSError):
            return
        self._discard_legacy(state_path)

    @staticmethod
    def _discard_legacy(*paths):
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass

    @staticmethod
    def validate_credential(doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("auth"), dict) or not isinstance(doc.get("account"), dict):
            raise HTTPException(400, "需要桌面端 .info／JSON 登录文件，包含 auth 和 account 对象")
        for key in ("accessToken", "refreshToken"):
            if not isinstance(doc["auth"].get(key), str) or not doc["auth"][key].strip():
                raise HTTPException(400, "凭据缺少 accessToken 或 refreshToken，请重新导出桌面端登录文件")
            if len(doc["auth"][key]) > 65536:
                raise HTTPException(400, "凭据字段过长")
        if not isinstance(doc["account"].get("uid"), str) or not doc["account"]["uid"]:
            raise HTTPException(400, "凭据缺少 account.uid")
        if not isinstance(doc["auth"].get("expiresAt"), (int, float)) or isinstance(doc["auth"].get("expiresAt"), bool):
            raise HTTPException(400, "凭据需要有效的 expiresAt 时间戳")
        for key in ("domain",):
            value = doc["auth"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "凭据包含无效字段")
        for key in ("uid", "enterpriseId"):
            value = doc["account"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "账号字段格式不正确")

    def save(self):
        self.db.save_state(self.data)

    def save_sessions(self):
        self.db.replace_sessions(self.sessions)

    def save_browser_account(self, doc, name):
        self.validate_credential(doc)
        with self.lock:
            identity = (doc["account"]["uid"], doc["account"].get("enterpriseId", ""))
            for aid, item in self.data["accounts"].items():
                try:
                    old = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if (old.get("account", {}).get("uid"), old.get("account", {}).get("enterpriseId", "")) == identity:
                    current = self.manager_for(aid, item)
                    if current:
                        with current._lock:
                            write_json(self.file_for(item), doc)
                            current._cached = None
                    else:
                        write_json(self.file_for(item), doc)
                    if name:
                        item["name"] = name
                    self.data.get("account_status", {}).pop(aid, None)
                    self.save()
                    self.sync_active()
                    return {"account_id": aid, "updated": True}
            if len(self.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            write_json(self.auth_dir / (aid + ".info"), doc)
            self.data["accounts"][aid] = {"file": aid + ".info", "name": name or str(doc["account"].get("nickname") or "浏览器授权账号")[:60], "created": int(time.time()), "enabled": True}
            if self.data["active"] is None:
                self.data["active"] = aid
            self.save()
            self.sync_active()
            return {"account_id": aid, "updated": False}

    def file_for(self, item):
        path = self.auth_dir / item["file"]
        if path.parent.resolve() != self.auth_dir.resolve() or path.is_symlink():
            raise HTTPException(400, "凭据路径不合法")
        return path

    def sync_active(self):
        aid = self.data.get("active")
        item = self.data["accounts"].get(aid)
        converter.CONFIG["cred"] = self.manager_for(aid, item) if item and item["enabled"] and self.file_for(item).exists() else None

    def manager_for(self, aid, item):
        with self.lock:
            if aid not in self.managers:
                self.managers[aid] = converter.CredentialManager(self.file_for(item))
            return self.managers[aid]

    def account_rows(self):
        result = []
        for aid, item in self.data["accounts"].items():
            row = {"id": aid, "name": item["name"], "enabled": item["enabled"], "active": aid == self.data["active"], "created": item["created"], "max_concurrent": int(item.get("max_concurrent") or 0)}
            try:
                doc = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                expiry = doc["auth"].get("expiresAt", 0)
                auth = doc["auth"]
                try:
                    profile = site_routing.profile_for_auth(auth)
                except ValueError:
                    profile = ""
                row.update({"nickname": str(doc["account"].get("nickname") or ""), "uid": str(doc["account"].get("uid") or ""), "expires_at": expiry, "expired": expiry < time.time() * 1000, "refresh_available": bool(doc["auth"].get("refreshToken")), "profile": profile, "domain": site_routing.domain_for_auth(auth), "status": "ready"})
            except (ValueError, OSError, KeyError):
                row.update({"status": "invalid", "expired": True, "expires_at": 0, "refresh_available": False, "profile": "", "domain": ""})
            result.append(row)
        return result

    def add_key(self, name, value=None):
        value = value or "sk-wb-" + secrets.token_urlsafe(32)
        kid = secrets.token_hex(8)
        self.data["keys"][kid] = {"name": name, "hash": digest(value), "hint": value[:5] + "…" + value[-4:], "created": int(time.time())}
        return kid, value

    def check_api(self, authorization, x_api_key):
        token = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else x_api_key or ""
        with self.lock:
            hashed = digest(token)
            allowed = hashed in self.test_keys or any(hmac.compare_digest(hashed, item["hash"]) for item in self.data["keys"].values())
        if not allowed:
            raise HTTPException(401, "invalid api key")

    def key_label(self, authorization, x_api_key):
        """把请求携带的密钥映射为展示名（不保留密钥本身）。

        命中测试密钥返回「连接测试」，命中客户端密钥返回其名称，
        其它情况返回空串。仅用于请求记录，不参与鉴权。
        """
        token = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else x_api_key or ""
        if not token:
            return ""
        hashed = digest(token)
        with self.lock:
            if hashed in self.test_keys:
                return "连接测试"
            for item in self.data["keys"].values():
                if hmac.compare_digest(hashed, item["hash"]):
                    return item.get("name") or "未命名密钥"
        return ""

    def require_admin(self, req):
        sid = req.cookies.get(COOKIE, "")
        with self.lock:
            record = self.sessions.get(digest(sid))
            if not record or record["expires"] < time.time():
                raise HTTPException(401, "请先登录管理后台")
        if req.method not in ("GET", "HEAD"):
            if not hmac.compare_digest(req.headers.get("X-CSRF-Token", ""), record["csrf"]):
                raise HTTPException(403, "页面已失效，请重新登录")
        return record


class AdminMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/admin"):
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                response = JSONResponse({"detail": "文件或请求超过 1 MB"}, 413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ]
            await send(message)
        await self.app(scope, replay, secure_send)


def resolve_store_paths(root=None, auth_dir=None):
    """统一优先级：显式参数 > 环境变量 > .env > 默认值（当前运行目录）。

    - 数据目录：root > ADMIN_DIR > MANAGEMENT_DATA_DIR > cwd
    - 凭据目录：auth_dir > CODEBUDDY_AUTH_DIR > cwd/auth
    """
    settings.ensure_loaded()
    data_dir = root or settings.path("ADMIN_DIR") or settings.path("MANAGEMENT_DATA_DIR") or settings.cwd()
    auth = auth_dir or settings.path("CODEBUDDY_AUTH_DIR") or settings.cwd() / "auth"
    return data_dir, auth


def create_app(root=None, auth_dir=None, initial_key=None, admin_key=None, secure_cookie=None, db=None):
    data_dir, auth = resolve_store_paths(root, auth_dir)
    if secure_cookie is None:
        secure_cookie = settings.get_bool("ADMIN_SECURE_COOKIE", False)
    key = initial_key if initial_key is not None else settings.get("CODEBUDDY2OPENAI_KEY", "")
    admin = admin_key or settings.get("ADMIN_KEY", "")
    database = db if db is not None else Database(Path(data_dir) / "mywb.db")
    store = Store(data_dir, auth, key, admin, db=database)
    converter._check_auth = store.check_api
    converter.CONFIG.update({"desensitize": True, "no_compact": False, "log_path": None})
    browser_login = BrowserLogin(store.save_browser_account)
    pool = AccountPool(store)
    metrics = RequestMetrics(database, legacy_path=store.root / "metrics.json")
    @asynccontextmanager
    async def lifespan(app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                await browser_login.cleanup()
        cleanup_task = asyncio.create_task(reap())
        async def persist():
            # Aggregates outlive the process; a short flush interval keeps at most
            # one interval of counters at risk on an abrupt shutdown.
            while True:
                await asyncio.sleep(60)
                await asyncio.to_thread(metrics.save)
        persist_task = asyncio.create_task(persist())
        async def pool_worker():
            while True:
                try:
                    await pool.tick()
                except Exception:
                    pass  # Per-account errors are persisted without credential data.
                await asyncio.sleep(60)
        pool_task = asyncio.create_task(pool_worker())
        try:
            yield
        finally:
            cleanup_task.cancel()
            pool_task.cancel()
            persist_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await browser_login.close()
            try:
                await pool_task
            except asyncio.CancelledError:
                pass
            try:
                await persist_task
            except asyncio.CancelledError:
                pass
            await asyncio.to_thread(metrics.save)
            if db is None:
                database.close()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.db = database
    app.state.secure_cookie = secure_cookie
    app.state.browser_login = browser_login
    app.state.pool = pool
    app.state.metrics = metrics
    app.add_middleware(AdminMiddleware)
    app.add_middleware(PoolMiddleware, pool=pool)
    app.add_middleware(
        MetricsMiddleware, metrics=metrics,
        key_label=lambda headers: store.key_label(
            headers.get(b"authorization", b"").decode("latin-1"),
            headers.get(b"x-api-key", b"").decode("latin-1"),
        ),
    )

    async def payload(req):
        try:
            body = await req.json()
        except ValueError:
            raise HTTPException(400, "请求不是有效 JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "请求格式不正确")
        return body

    @app.get("/")
    async def home():
        return RedirectResponse("/admin/", 302)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/admin")
    async def redirect_admin():
        return RedirectResponse("/admin/", 302)

    @app.get("/admin/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/admin/assets/{filename}")
    async def asset(filename: str):
        # Vue 构建产物（app.js / style.css 及可能的附加静态文件）统一落在 ASSETS 根目录。
        # 用真实路径做父目录校验，避免 ../ 穿越；不存在的文件仍返回 404。
        path = (ASSETS / filename).resolve()
        if path.parent != ASSETS.resolve() or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.post("/admin/api/login")
    async def login(req: Request):
        body = await payload(req)
        # Nginx overwrites X-Real-IP; the application port is loopback-only.
        ip = req.headers.get("x-real-ip") or (req.client.host if req.client else "unknown")
        now = time.time()
        with store.lock:
            store.attempts = {k: v for k, v in store.attempts.items() if v[-1] > now - 600}
            failures = [t for t in store.attempts.get(ip, []) if t > now - 600]
            if len(failures) >= 8:
                raise HTTPException(429, "尝试次数过多，请 10 分钟后重试")
            value = body.get("key")
            if not isinstance(value, str) or not hmac.compare_digest(digest(value), store.admin_digest):
                store.attempts[ip] = failures + [now]
                raise HTTPException(401, "管理密钥不正确")
            store.attempts.pop(ip, None)
            store.sessions = {k: v for k, v in store.sessions.items() if v["expires"] > now}
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            if len(store.sessions) >= 100:
                store.sessions.pop(next(iter(store.sessions)))
            store.sessions[digest(sid)] = {"csrf": csrf, "expires": now + 12 * 3600}
            store.save_sessions()
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(COOKIE, sid, secure=secure_cookie, httponly=True, samesite="strict", path="/admin", max_age=12 * 3600)
        return response

    @app.get("/admin/api/session")
    async def session(req: Request):
        return {"csrf": store.require_admin(req)["csrf"]}

    @app.post("/admin/api/logout")
    async def logout(req: Request):
        store.require_admin(req)
        await browser_login.cancel_owner(digest(req.cookies.get(COOKIE, "")))
        with store.lock:
            store.sessions.pop(digest(req.cookies.get(COOKIE, "")), None)
            store.save_sessions()
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/admin", secure=secure_cookie, httponly=True, samesite="strict")
        return response

    @app.post("/admin/api/oauth/start")
    async def oauth_start(req: Request):
        store.require_admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), None)
        site = body.get("site", DEFAULT_SITE)
        if site not in SITE_HOSTS:
            raise HTTPException(400, "请选择支持的登录站点")
        try:
            return await browser_login.start(digest(req.cookies.get(COOKIE, "")), name, site)
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权服务连接失败，请稍后重试")

    @app.post("/admin/api/oauth/{fid}/poll")
    async def oauth_poll(fid: str, req: Request):
        store.require_admin(req)
        try:
            return await browser_login.poll(fid, digest(req.cookies.get(COOKIE, "")))
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权状态暂时无法获取，请稍后重试")

    @app.delete("/admin/api/oauth/{fid}")
    async def oauth_cancel(fid: str, req: Request):
        store.require_admin(req)
        return await browser_login.cancel(fid, digest(req.cookies.get(COOKIE, "")))

    @app.get("/admin/api/overview")
    async def overview(req: Request):
        store.require_admin(req)
        with store.lock:
            keys = [{"id": kid, **{k: v for k, v in item.items() if k != "hash"}} for kid, item in store.data["keys"].items()]
            return {"accounts": pool.rows(store.account_rows()), "pool": dict(store.data["pool"]), "metrics": metrics.snapshot(), "keys": keys, "models": converter.get_available_models(), "uptime": int(time.time() - store.started), "events": list(store.events)}

    @app.get("/admin/api/stats")
    async def stats(req: Request):
        store.require_admin(req)
        with store.lock:
            return metrics.stats()

    @app.get("/admin/api/requests")
    async def requests(req: Request):
        store.require_admin(req)
        return {"requests": metrics.snapshot()["recent"]}

    @app.post("/admin/api/accounts/{aid}/actions/{action}")
    async def account_action(aid: str, action: str, req: Request):
        store.require_admin(req)
        if action not in ("refresh", "status", "checkin"):
            raise HTTPException(404)
        return await asyncio.to_thread(pool.operate, aid, action)

    @app.post("/admin/api/pool/actions/{action}")
    async def pool_action(action: str, req: Request):
        store.require_admin(req)
        if action not in ("status", "checkin"):
            raise HTTPException(404)
        return await pool.batch(action)

    @app.patch("/admin/api/pool/settings")
    async def pool_settings(req: Request):
        store.require_admin(req)
        body = await payload(req)
        import re
        with store.lock:
            settings = dict(store.data["pool"])
            if "routing" in body:
                if body["routing"] not in ("manual", "round_robin"):
                    raise HTTPException(400, "调度方式无效")
                settings["routing"] = body["routing"]
            if "auto_checkin" in body:
                if type(body["auto_checkin"]) is not bool:
                    raise HTTPException(400, "签到开关无效")
                settings["auto_checkin"] = body["auto_checkin"]
            if "checkin_time" in body:
                if not isinstance(body["checkin_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", body["checkin_time"]):
                    raise HTTPException(400, "请选择有效的签到时间")
                settings["checkin_time"] = body["checkin_time"]
            store.data["pool"] = settings
            store.save()
        return {"ok": True, "pool": settings}

    @app.post("/admin/api/accounts")
    async def add_account(req: Request):
        store.require_admin(req)
        body = await payload(req)
        doc = body.get("credential")
        store.validate_credential(doc)
        name = clean_name(body.get("name"), str(doc["account"].get("nickname") or "新账号")[:60])
        with store.lock:
            if len(store.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            filename = aid + ".info"
            write_json(store.auth_dir / filename, doc)
            store.data["accounts"][aid] = {"file": filename, "name": name, "created": int(time.time()), "enabled": True, "max_concurrent": 0}
            if store.data["active"] is None:
                store.data["active"] = aid
            store.save()
            store.sync_active()
        return {"id": aid}

    @app.patch("/admin/api/accounts/{aid}")
    async def edit_account(aid: str, req: Request):
        store.require_admin(req)
        body = await payload(req)
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            new = dict(item)
            if "name" in body:
                new["name"] = clean_name(body["name"], item["name"])
            if "enabled" in body:
                if type(body["enabled"]) is not bool:
                    raise HTTPException(400, "启用状态不正确")
                new["enabled"] = body["enabled"]
            if "max_concurrent" in body:
                value = body["max_concurrent"]
                if type(value) is not int or isinstance(value, bool) or not 0 <= value <= 1000:
                    raise HTTPException(400, "并发上限需为 0–1000 的整数（0 表示不限）")
                new["max_concurrent"] = value
            if body.get("active") is True:
                if not new["enabled"]:
                    raise HTTPException(400, "请先启用账号")
                try:
                    store.validate_credential(json.loads(store.file_for(new).read_text(encoding="utf-8")))
                except (ValueError, OSError):
                    raise HTTPException(400, "凭据文件无法读取，请重新导入")
                store.data["active"] = aid
            store.data["accounts"][aid] = new
            if not new["enabled"] and store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
        return {"ok": True}

    @app.delete("/admin/api/accounts/{aid}")
    async def delete_account(aid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            trash = store.root / "trash" / (aid + "-" + secrets.token_hex(4))
            trash.mkdir(parents=True, mode=0o700)
            source = store.file_for(item)
            if source.exists():
                write_json(trash / "credential.json", json.loads(source.read_text(encoding="utf-8")))
            write_json(trash / "metadata.json", item)
            del store.data["accounts"][aid]
            if store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
            # Keep the file so an in-flight token refresh may still complete safely.
            # Account storage is authoritative; deleted records cannot be selected.
        return {"ok": True}

    @app.post("/admin/api/keys")
    async def add_key(req: Request):
        store.require_admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), "新客户端")
        with store.lock:
            if len(store.data["keys"]) >= 100:
                raise HTTPException(400, "最多保存 100 枚 API Key")
            kid, key = store.add_key(name)
            store.save()
        return {"id": kid, "key": key}

    @app.delete("/admin/api/keys/{kid}")
    async def revoke_key(kid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            if kid not in store.data["keys"]:
                raise HTTPException(404, "密钥不存在")
            if len(store.data["keys"]) <= 1:
                raise HTTPException(400, "请先创建一枚新密钥，再撤销最后一枚密钥")
            del store.data["keys"][kid]
            store.save()
        return {"ok": True}

    @app.post("/admin/api/test")
    async def test_model(req: Request):
        store.require_admin(req)
        body = await payload(req)
        model = body.get("model", "deepseek-v4-flash")
        prompt = body.get("prompt", "请只回复：连接成功")
        if not isinstance(model, str) or model not in converter.get_available_models():
            raise HTTPException(400, "请选择列表中的模型")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            raise HTTPException(400, "测试消息需为 1–2000 个字符")
        if store.test_lock.locked():
            raise HTTPException(409, "已有测试正在进行")
        if converter.CONFIG.get("cred") is None:
            raise HTTPException(400, "请先导入并启用一个账号")
        async with store.test_lock:
            started = time.monotonic()
            # Use a short-lived API key with the same authentication path as clients.
            with store.lock:
                temp_key = secrets.token_urlsafe(48)
                store.test_keys.add(digest(temp_key))
            try:
                test_account = pool.account_label(store.data.get("active")) if store.data.get("active") else ""
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=MetricsMiddleware(converter.app, metrics, source="test", account=test_account)), base_url="http://internal") as client:
                    result = await asyncio.wait_for(client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + temp_key}, json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1024, "stream": False}), timeout=90)
                data = result.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content", "") if result.status_code == 200 else ""
                output = {"ok": result.status_code == 200 and bool(answer), "status": result.status_code, "answer": answer, "seconds": round(time.monotonic() - started, 2), "usage": data.get("usage") if result.status_code == 200 else None}
                if not output["ok"]:
                    output["error"] = "上游未返回有效回复，请检查登录凭据、账号额度或模型权限。"
                    if result.status_code == 200 and data.get("choices", [{}])[0].get("finish_reason") == "length":
                        output["error"] = "已连接上游，但生成预算耗尽，未获得正文。请在客户端提高 max_tokens 后重试。"
            except (Exception, asyncio.TimeoutError):
                output = {"ok": False, "error": "调用失败或超时，请检查凭据是否有效，稍后重试。", "seconds": round(time.monotonic() - started, 2)}
            finally:
                with store.lock:
                    store.test_keys.discard(digest(temp_key))
            store.events.appendleft({"time": int(time.time()), "model": model, "ok": output["ok"], "seconds": output["seconds"]})
            return output

    app.mount("/", converter.app)
    return app


if __name__ == "__main__":
    settings.ensure_loaded()
    uvicorn.run(create_app(), host="0.0.0.0", port=settings.get_int("PORT", 8787), log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
