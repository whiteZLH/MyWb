"""内置 VLESS 出站：链接解析、配置生成、进程生命周期（用假内核验证）。"""

import json
import socket

import pytest

from core import vless

# 用户实测可用的真实分享链接（ws + tls + Host + path + fp=ios + sni）
LINK = (
    "vless://ca997d60-d8db-4eda-8403-1abd8ea8fcc3@unamecf.xn--ghqu5fm27b67w.com:443"
    "?type=ws&encryption=none&host=ujp1.xn--ghqu5fm27b67w.com&path=%2Fpq%2Fjp1"
    "&headerType=none&quicSecurity=none&serviceName=&security=tls&fp=ios&insecure=0"
    "&sni=ujp1.xn--ghqu5fm27b67w.com#%E5%89%A9%E4%BD%99%E6%B5%81%E9%87%8F"
)


def test_parse_real_ws_tls_link():
    out = vless.parse_vless_link(LINK)
    assert out["type"] == "vless"
    assert out["server"] == "unamecf.xn--ghqu5fm27b67w.com"
    assert out["server_port"] == 443
    assert out["uuid"] == "ca997d60-d8db-4eda-8403-1abd8ea8fcc3"
    assert out["transport"] == {
        "type": "ws",
        "path": "/pq/jp1",
        "headers": {"Host": "ujp1.xn--ghqu5fm27b67w.com"},
    }
    assert out["tls"]["enabled"] is True
    assert out["tls"]["server_name"] == "ujp1.xn--ghqu5fm27b67w.com"
    assert out["tls"]["utls"] == {"enabled": True, "fingerprint": "ios"}
    assert "insecure" not in out["tls"]


def test_parse_reality_and_grpc():
    link = ("vless://11111111-2222-3333-4444-555555555555@1.2.3.4:8443"
            "?type=grpc&security=reality&serviceName=svc&sni=example.com"
            "&pbk=PUBKEY&sid=abcd&flow=xtls-rprx-vision&fp=chrome")
    out = vless.parse_vless_link(link)
    assert out["flow"] == "xtls-rprx-vision"
    assert out["transport"] == {"type": "grpc", "service_name": "svc"}
    assert out["tls"]["reality"] == {"enabled": True, "public_key": "PUBKEY", "short_id": "abcd"}


def test_parse_rejects_invalid():
    for bad in ["http://x", "vless://", "vless://uuid@host"]:
        with pytest.raises(ValueError):
            vless.parse_vless_link(bad)


def test_build_config_multi_node():
    nodes = [(LINK, 20808), (LINK.replace("443", "8443"), 20809)]
    config = vless.build_config(nodes)
    assert [ib["listen_port"] for ib in config["inbounds"]] == [20808, 20809]
    assert config["inbounds"][0]["type"] == "mixed"
    tags = [ob["tag"] for ob in config["outbounds"]]
    assert tags == ["node-0", "node-1", "direct"]
    assert config["route"]["rules"][1]["inbound"] == ["in-1"]
    assert config["route"]["rules"][1]["outbound"] == "node-1"
    assert config["route"]["final"] == "direct"


def test_resolve_passthrough_and_empty():
    assert vless.resolve("http://127.0.0.1:7897") == "http://127.0.0.1:7897"
    assert vless.resolve("socks5://h:1") == "socks5://h:1"
    assert vless.resolve("") is None
    assert vless.resolve(None) is None


class _FakeProc:
    """假 sing-box：读配置、绑好每个入站端口、等待被终止。"""

    def __init__(self, args, **kwargs):
        config_path = args[args.index("-c") + 1]
        with open(config_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.sockets = []
        for inbound in data["inbounds"]:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", inbound["listen_port"]))
            sock.listen(1)
            self.sockets.append(sock)
        self._rc = None

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0
        for sock in self.sockets:
            sock.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return 0


def test_lifecycle_start_and_stop(monkeypatch, tmp_path):
    binary = tmp_path / "sing-box"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(vless.subprocess, "Popen", _FakeProc)

    nodes = vless.VlessNodes(base_port=21500, binary=str(binary), ready_timeout=5.0)
    try:
        url = nodes.resolve(LINK)
        assert url == "http://127.0.0.1:21500"
        assert nodes.status()["running"] is True
        # 幂等：同一链接返回同一端口，不重复起进程。
        assert nodes.resolve(LINK) == url
        assert len(nodes.status()["nodes"]) == 1
    finally:
        nodes.stop()
    assert nodes.status()["running"] is False


def test_missing_binary_raises_and_rolls_back(monkeypatch, tmp_path):
    nodes = vless.VlessNodes(base_port=21600, binary=str(tmp_path / "nope"), ready_timeout=1.0)
    with pytest.raises(RuntimeError):
        nodes.resolve(LINK)
    assert nodes.status()["nodes"] == []


def test_probe_records_health(monkeypatch):
    import httpx

    real_client = httpx.Client
    monkeypatch.setattr(
        vless.httpx, "Client",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="203.0.113.9\n"))),
    )
    nodes = vless.VlessNodes(binary="x")
    monkeypatch.setattr(nodes, "resolve", lambda link: "http://127.0.0.1:1")

    result = nodes.probe(LINK)
    assert result["ok"] is True
    assert result["ip"] == "203.0.113.9"
    assert nodes.health[LINK]["ok"] is True
    assert nodes.status()["nodes"] == []  # 未加入 sing-box，仅记录健康


def test_probe_failure_is_recorded(monkeypatch):
    import httpx

    real_client = httpx.Client
    monkeypatch.setattr(
        vless.httpx, "Client",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(lambda req: httpx.Response(502, text="bad"))),
    )
    nodes = vless.VlessNodes(binary="x")
    monkeypatch.setattr(nodes, "resolve", lambda link: "http://127.0.0.1:1")

    result = nodes.probe(LINK)
    assert result["ok"] is False
    assert "HTTP 502" in result["error"]


def test_probe_resolve_failure(monkeypatch):
    nodes = vless.VlessNodes(binary="nope")

    def boom(link):
        raise RuntimeError("未找到 sing-box 内核")

    monkeypatch.setattr(nodes, "resolve", boom)
    result = nodes.probe(LINK)
    assert result["ok"] is False
    assert "sing-box" in result["error"]
