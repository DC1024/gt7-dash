"""传输层：gzip 与 keep-alive。

这两件事必须**连真的一次**才测得出来：

- HTTP/1.1 长连接下 Content-Length 写错，客户端会一直等剩下的字节
  （表现为页面转圈不结束，服务端日志一切正常）——单测 `_send_json` 看不出来。
- gzip 之后字节数就变了，所以「压缩」和「写长度」必须同一处算，
  这里直接把 Content-Length 与实际收到的字节数对一遍。
"""
import gzip
import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture(scope="module")
def srv(dash, tmp_path_factory):
    hist = tmp_path_factory.mktemp("data")

    class Handler(dash.DashboardHandler):
        pass

    # 端口 0 = 让内核挑一个空闲端口，免得和本机跑着的实例抢 8787
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.history_dir = str(hist)  # type: ignore[attr-defined]
    # HUB 平时由 main() 建；这里没有 main，不建的话 /api/health 会 500
    dash.HUB = dash.TelemetryHub(hist / "status.json")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def port(srv):
    return srv.server_address[1]


def _get(port, path, headers=None, conn=None):
    """发一个请求，返回 (response, body)。conn 给了就复用它（测长连接用）。"""
    own = conn is None
    if own:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        return r, body
    finally:
        if own:
            conn.close()


class TestKeepAlive:
    def test_同一连接能连发两个请求(self, port):
        """keep-alive 没开时，第二个请求会撞上服务端主动断开。"""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            r1, b1 = _get(port, "/api/health", conn=conn)
            assert r1.status == 200, b1
            assert r1.getheader("Content-Length") == str(len(b1))
            r2, b2 = _get(port, "/api/health", conn=conn)
            assert r2.status == 200, b2
        finally:
            conn.close()

    def test_客户端要求关闭时服务端照办(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/api/health",
                         headers={"Connection": "close"})
            r = conn.getresponse()
            r.read()
            assert r.getheader("Connection") == "close"
        finally:
            conn.close()


class TestGzip:
    def test_声明_gzip_时才压(self, port):
        r, body = _get(port, "/api/v1/docs",
                       {"Accept-Encoding": "gzip"})
        assert r.status == 200
        assert r.getheader("Content-Encoding") == "gzip"
        # 🔴 这条是重点：长度必须是**压完**的字节数
        assert r.getheader("Content-Length") == str(len(body))
        assert r.getheader("Vary") == "Accept-Encoding"
        text = gzip.decompress(body).decode("utf-8")
        assert text.startswith("# GT7 遥测公开 API v1")

    def test_不声明就不压_且内容一致(self, port):
        r, plain = _get(port, "/api/v1/docs", {"Accept-Encoding": "identity"})
        assert r.status == 200
        assert r.getheader("Content-Encoding") is None
        assert r.getheader("Content-Length") == str(len(plain))
        _, body = _get(port, "/api/v1/docs", {"Accept-Encoding": "gzip"})
        assert gzip.decompress(body) == plain      # 压缩前后逐字节一致

    def test_q0_显式拒绝时不压(self, port):
        r, body = _get(port, "/api/v1/docs", {"Accept-Encoding": "gzip;q=0"})
        assert r.getheader("Content-Encoding") is None
        assert body.startswith(b"# GT7")

    def test_小响应不压(self, port, dash):
        """几百字节压了也省不了多少，白花 CPU —— 阈值以下直接放行。"""
        r, body = _get(port, "/api/health", {"Accept-Encoding": "gzip"})
        assert r.status == 200
        assert len(body) < dash._GZIP_MIN_BYTES, "这条响应已经大到该压了，换个端点"
        assert r.getheader("Content-Encoding") is None
