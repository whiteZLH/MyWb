"""内置 VLESS 出站：把 ``vless://`` 分享链接变成本地 HTTP 代理。

目标：**给每个账号配不同节点 → 不同出站 IP**。做法是在本机起一个 sing-box，
为每个唯一节点开一个 ``127.0.0.1`` 的 mixed 入站，账号的出站代理指向对应端口。

- 零新 Python 依赖；内核用镜像内置的静态 ``sing-box``（见 deploy 的 Dockerfile），
  或用 ``CODEBUDDY_VLESS_BIN`` 指定路径（默认按 PATH / /usr/local/bin 查找）。
- 单进程多入站：N 个节点 → 1 个 sing-box 进程，路由规则 inbound_i → outbound_i。
- 版本/线程安全：所有注册与重启在锁内串行；``resolve`` 命中已存在节点时零开销。
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from . import settings

DEFAULT_BASE_PORT = 20808
MAX_NODES = 32
READY_TIMEOUT = 10.0
# 探测用：返回纯文本出口 IP，经节点请求成功即视为可用。
PROBE_URL = "https://api.ipify.org"

_TRUTHY = ("1", "true", "yes", "on")


def is_vless(value) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith("vless://")


def _truthy(value) -> bool:
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _first(query: dict, *names):
    for name in names:
        value = query.get(name)
        if value:
            return value
    return ""


def parse_vless_link(link: str) -> dict:
    """把 ``vless://`` 分享链接解析成 sing-box 的 vless outbound（不含 tag）。"""
    if not is_vless(link):
        raise ValueError("不是有效的 vless:// 链接")
    parts = urlsplit(link.strip())
    uuid = unquote(parts.username or "")
    server = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        raise ValueError("vless 链接端口无效")
    if not uuid or not server or not port:
        raise ValueError("vless 链接缺少 uuid / host / port")

    query = {key.lower(): values[0] for key, values in parse_qs(parts.query).items()}
    transport = (_first(query, "type") or "tcp").lower()
    security = (_first(query, "security") or "none").lower()
    host_header = _first(query, "host")
    path = _first(query, "path")
    if path:
        path = unquote(path)
    sni = _first(query, "sni", "peer") or ("" if _is_ip(server) else server)

    outbound: dict = {"type": "vless", "server": server, "server_port": int(port), "uuid": uuid}
    flow = _first(query, "flow")
    if flow:
        outbound["flow"] = flow

    if transport == "ws":
        item: dict = {"type": "ws"}
        if path:
            item["path"] = path
        if host_header:
            item["headers"] = {"Host": host_header}
        outbound["transport"] = item
    elif transport == "grpc":
        outbound["transport"] = {"type": "grpc", "service_name": _first(query, "servicename", "servicename")}
    elif transport in ("http", "h2"):
        item = {"type": "http"}
        if host_header:
            item["host"] = [h for h in host_header.split(",") if h]
        if path:
            item["path"] = path
        outbound["transport"] = item

    if security in ("tls", "reality", "xtls"):
        tls: dict = {"enabled": True}
        if sni:
            tls["server_name"] = sni
        alpn = _first(query, "alpn")
        if alpn:
            tls["alpn"] = [a for a in alpn.split(",") if a]
        if _truthy(_first(query, "allowinsecure", "insecure")):
            tls["insecure"] = True
        fingerprint = _first(query, "fp")
        if fingerprint:
            tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
        if security == "reality":
            tls["reality"] = {
                "enabled": True,
                "public_key": _first(query, "pbk"),
                "short_id": _first(query, "sid"),
            }
        outbound["tls"] = tls
    return outbound


def build_config(nodes: list[tuple[str, int]]) -> dict:
    """``[(link, port), ...]`` → 单进程 sing-box 配置（每节点一个 mixed 入站）。"""
    inbounds, outbounds, rules = [], [], []
    for index, (link, port) in enumerate(nodes):
        inbounds.append({
            "type": "mixed",
            "tag": f"in-{index}",
            "listen": "127.0.0.1",
            "listen_port": int(port),
        })
        outbound = parse_vless_link(link)
        outbound["tag"] = f"node-{index}"
        outbounds.append(outbound)
        rules.append({"inbound": [f"in-{index}"], "outbound": f"node-{index}"})
    outbounds.append({"type": "direct", "tag": "direct"})
    return {
        "log": {"level": "warn"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {"rules": rules, "final": "direct"},
    }


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class VlessNodes:
    """进程级 VLESS 节点管理器（单 sing-box，多入站）。"""

    def __init__(self, base_port: int = DEFAULT_BASE_PORT, binary: str | None = None,
                 max_nodes: int = MAX_NODES, ready_timeout: float = READY_TIMEOUT):
        self.base_port = base_port
        self._binary = binary
        self.max_nodes = max_nodes
        self.ready_timeout = ready_timeout
        self._lock = threading.RLock()
        self._ports: dict[str, int] = {}
        self._order: list[str] = []
        self.health: dict[str, dict] = {}
        self._proc: subprocess.Popen | None = None
        self._dir: str | None = None
        self._config_path: str | None = None
        self.error: str | None = None

    # ---------------------------------------------------------------- 二进制

    def binary_path(self) -> str | None:
        if self._binary and os.path.exists(self._binary):
            return self._binary
        configured = (settings.get("CODEBUDDY_VLESS_BIN") or "").strip()
        if configured and os.path.exists(configured):
            return configured
        found = shutil.which("sing-box")
        if found:
            return found
        for path in ("/usr/local/bin/sing-box", "/usr/bin/sing-box"):
            if os.path.exists(path):
                return path
        return None

    # ---------------------------------------------------------------- 对外接口

    def resolve(self, link: str) -> str:
        """确保该节点已就绪，返回它的本地 HTTP 代理地址。"""
        link = link.strip()
        with self._lock:
            existing = self._ports.get(link)
            if existing is not None:
                return f"http://127.0.0.1:{existing}"
            if len(self._order) >= self.max_nodes:
                raise RuntimeError(f"VLESS 节点数超过上限（{self.max_nodes}）")
            port = self._allocate_port()
            self._order.append(link)
            self._ports[link] = port
            try:
                self._apply()
            except Exception:
                self._order.pop()
                self._ports.pop(link, None)
                raise
            return f"http://127.0.0.1:{port}"

    def probe(self, link: str, url: str = PROBE_URL, timeout: float = 15.0) -> dict:
        """经该节点发一次探测请求，记录可用性 / 延迟 / 出口 IP。"""
        link = link.strip()
        result: dict = {"ok": False, "ms": None, "ip": None, "error": None,
                        "checked_at": int(time.time())}
        try:
            proxy = self.resolve(link)
        except Exception as exc:  # noqa: BLE001 - 节点不可用是预期结果
            result["error"] = str(exc)[:160]
            self.health[link] = result
            return result
        started = time.time()
        try:
            with httpx.Client(timeout=timeout, proxy=proxy) as client:
                response = client.get(url)
            result["ms"] = int((time.time() - started) * 1000)
            if response.status_code == 200:
                result["ok"] = True
                result["ip"] = response.text.strip()[:64]
            else:
                result["error"] = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            result["ms"] = int((time.time() - started) * 1000)
            result["error"] = f"{type(exc).__name__}: {exc}"[:160]
        self.health[link] = result
        return result
    def port_for(self, link):
        """该节点已分配的本地端口（未起过则为 None）。"""
        with self._lock:
            return self._ports.get((link or "").strip())

    def status(self) -> dict:
        with self._lock:
            nodes = []
            for index, link in enumerate(self._order):
                try:
                    outbound = parse_vless_link(link)
                    label = f"{outbound['server']}:{outbound['server_port']}"
                except ValueError:
                    label = "(无效链接)"
                nodes.append({"index": index, "label": label, "port": self._ports[link],
                              "health": self.health.get(link)})
            return {
                "running": self._proc is not None and self._proc.poll() is None,
                "nodes": nodes,
                "error": self.error,
            }

    def stop(self) -> None:
        with self._lock:
            self._stop_proc()
            if self._dir and os.path.isdir(self._dir):
                shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
            self._config_path = None

    # ---------------------------------------------------------------- 内部

    def _allocate_port(self) -> int:
        for offset in range(self.max_nodes * 2):
            candidate = self.base_port + len(self._order) + offset
            if _port_free(candidate):
                return candidate
        raise RuntimeError("找不到可用的本地端口")

    def _apply(self) -> None:
        binary = self.binary_path()
        if not binary:
            self.error = "未找到 sing-box 内核（镜像应内置，或设置 CODEBUDDY_VLESS_BIN）"
            raise RuntimeError(self.error)
        config = build_config([(link, self._ports[link]) for link in self._order])
        if self._dir is None:
            self._dir = tempfile.mkdtemp(prefix="mywb-vless-")
        self._config_path = os.path.join(self._dir, "config.json")
        with open(self._config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False)
        self._restart(binary)
        self.error = None

    def _restart(self, binary: str) -> None:
        self._stop_proc()
        log_path = os.path.join(self._dir or tempfile.gettempdir(), "singbox.log")
        self._log = open(log_path, "a", encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                [binary, "run", "-c", self._config_path],
                stdout=subprocess.DEVNULL,
                stderr=self._log,
                close_fds=True,
            )
        except OSError as exc:
            self.error = f"sing-box 启动失败：{exc}"
            raise RuntimeError(self.error)
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.time() + self.ready_timeout
        ports = [self._ports[link] for link in self._order]
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                self.error = "sing-box 启动后立即退出（配置错误？详见状日志）"
                raise RuntimeError(self.error)
            if all(self._connectable(port) for port in ports):
                return
            time.sleep(0.15)
        self.error = "sing-box 就绪超时"
        raise RuntimeError(self.error)

    @staticmethod
    def _connectable(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    def _stop_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log = getattr(self, "_log", None)
        if log is not None:
            try:
                log.close()
            except OSError:
                pass
            self._log = None


# 进程级单例
NODES = VlessNodes()


def resolve(value) -> str | None:
    """解析出口代理值：``vless://`` → 本地代理地址；其余原样返回；空 → None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if is_vless(text):
        return NODES.resolve(text)
    return text


def default_proxy() -> str | None:
    """全局默认节点：``CODEBUDDY_EGRESS_VLESS``（取第一条链接）。"""
    raw = settings.get("CODEBUDDY_EGRESS_VLESS")
    if not raw:
        return None
    for line in raw.replace(",", "\n").splitlines():
        line = line.strip()
        if is_vless(line):
            return NODES.resolve(line)
    return None


def warmup() -> None:
    """启动时预热全局节点；失败只记 error，不阻断启动。"""
    try:
        default_proxy()
    except Exception as exc:  # noqa: BLE001 - 启动期不致命
        NODES.error = str(exc)


def shutdown() -> None:
    NODES.stop()
