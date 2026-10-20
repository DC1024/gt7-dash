# -*- coding: utf-8 -*-
"""跨站防护（P0-3）：局域网可达 + 挡跨站写 + Host 白名单。

守护的三件事，任何一件被简化都会出事：

  1. **局域网设备必须能直接用**。平板/手机打开页面后点收藏、删除、改名
     都要成功 —— 所以绝不能用"只允许 localhost"或"必须带 token"的方案。
  2. **别的网站的页面不能借你浏览器的手写数据**。浏览器对跨站 POST 是
     "简单请求"（不触发预检），恶意页面可以直接 `fetch(url, {method:'POST'})`，
     它读不到响应，但**写操作已经发生**——这就是 CSRF。
  3. **恶意域名不能读内网数据**。`evil.com` 解析到 192.168.x.x 时，
     请求的 Host 是 `evil.com` —— 这是 DNS rebinding，靠 Host 白名单挡。
"""
import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture(scope="module")
def srv(dash, tmp_path_factory):
    hist = tmp_path_factory.mktemp("data_csrf")

    class Handler(dash.DashboardHandler):
        pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.history_dir = str(hist)          # type: ignore[attr-defined]
    server.allow_hosts = ()                  # type: ignore[attr-defined]
    dash.HUB = dash.TelemetryHub(hist / "status.json")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def port(srv):
    return srv.server_address[1]


def _req(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r, r.read()
    finally:
        conn.close()


SETTINGS = "/api/v1/settings/trash-retention"
PAYLOAD = '{"value": 30}'


class TestLanAccessStillWorks:
    """局域网内正常使用**不能**被防护误伤——这是本项目的前提。"""

    def test_同源写操作照旧成功(self, port):
        """浏览器直接打开页面 = 同源：Origin 的 netloc 与 Host 一致。"""
        r, body = _req(port, "POST", SETTINGS, {
            "Content-Type": "application/json",
            "Content-Length": str(len(PAYLOAD)),
            "Host": "127.0.0.1:%d" % port,
            "Origin": "http://127.0.0.1:%d" % port,
            "Sec-Fetch-Site": "same-origin",
        }, PAYLOAD)
        assert r.status == 200, body

    def test_局域网_ip_访问照旧成功(self, port):
        """平板用 http://192.168.43.18:8787 打开 → Origin/Host 都是这个 IP。"""
        r, body = _req(port, "POST", SETTINGS, {
            "Content-Type": "application/json",
            "Content-Length": str(len(PAYLOAD)),
            "Host": "192.168.43.18:8787",
            "Origin": "http://192.168.43.18:8787",
            "Sec-Fetch-Site": "same-origin",
        }, PAYLOAD)
        assert r.status == 200, body

    def test_curl_脚本_没有_origin_也放行(self, port):
        """curl / Python 脚本不发 Origin，不是浏览器跨站场景。"""
        r, body = _req(port, "POST", SETTINGS, {
            "Content-Type": "application/json",
            "Content-Length": str(len(PAYLOAD)),
        }, PAYLOAD)
        assert r.status == 200, body

    def test_旧浏览器不发_sec_fetch_site_也放行(self, port):
        """旧版 Safari / 部分 Android WebView 不发 Sec-Fetch-Site。

        🔴 写成"缺失即拒绝"会让平板莫名 403 —— 这是本条测试守的底线。
        """
        r, body = _req(port, "POST", SETTINGS, {
            "Content-Type": "application/json",
            "Content-Length": str(len(PAYLOAD)),
            "Host": "192.168.43.18:8787",
            "Origin": "http://192.168.43.18:8787",
        }, PAYLOAD)
        assert r.status == 200, body

    def test_第三方网页读接口仍可跨域(self, port):
        """GET 保留 ACAO:*（文档宣传的公开读能力，别一刀切掉）。"""
        r, _ = _req(port, "GET", "/api/v1/laps")
        assert r.getheader("Access-Control-Allow-Origin") == "*"


class TestCrossSiteBlocked:
    """恶意网页发起的写操作必须被挡。"""

    def test_跨站_origin_被拒(self, port):
        r, body = _req(port, "POST", SETTINGS, {
            "Content-Type": "application/json",
            "Content-Length": str(len(PAYLOAD)),
            "Host": "192.168.43.18:8787",
            "Origin": "http://evil.example",
        }, PAYLOAD)
        assert r.status == 403, body
        assert "跨站" in body.decode("utf-8", "replace")

    def test_跨站_sec_fetch_site_被拒(self, port):
        r, body = _req(port, "POST", SETTINGS, {
            "Content-Type": "application/json",
            "Content-Length": str(len(PAYLOAD)),
            "Host": "192.168.43.18:8787",
            "Origin": "http://192.168.43.18:8787",   # 伪造一致的 Origin
            "Sec-Fetch-Site": "cross-site",           # 但浏览器说了实话
        }, PAYLOAD)
        assert r.status == 403, body

    def test_写响应不带通配_CORS(self, port):
        """就算被拒，也不能回 ACAO:*（否则前端能读到拒绝原因并继续探测）。"""
        r, _ = _req(port, "POST", SETTINGS, {
            "Content-Type": "application/json",
            "Content-Length": str(len(PAYLOAD)),
            "Host": "192.168.43.18:8787",
            "Origin": "http://evil.example",
        }, PAYLOAD)
        assert r.getheader("Access-Control-Allow-Origin") is None

    def test_预检请求不批准_POST(self, port):
        """跨站写前浏览器会先发 OPTIONS，这里必须不批准 POST。"""
        r, _ = _req(port, "OPTIONS", SETTINGS, {
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        })
        assert r.status == 403
        assert r.getheader("Access-Control-Allow-Methods") is None

    def test_预检请求批准_GET(self, port):
        r, _ = _req(port, "OPTIONS", "/api/v1/laps", {
            "Origin": "http://someone.example",
            "Access-Control-Request-Method": "GET",
        })
        assert r.status == 204
        assert r.getheader("Access-Control-Allow-Origin") == "*"


class TestRejectedRequestKeepsConnectionSane:
    """被拒的请求不能污染同一条长连接。

    🔴 这是提前 return 最容易踩的坑：`do_POST` 在跨站闸上直接 `return 403`，
       但 `self.rfile` 里那条请求的 body **一个字节都没读**。HTTP/1.1 长连接
       不会因为回了 403 就重置，于是残留的 `{"value": 30}` 会被
       `BaseHTTPRequestHandler` 当成**下一条请求行**解析：

           [..] 127.0.0.1 "{"value": 30}" 400 -

       表现为：浏览器对该主机的下一个请求莫名 400（而且是随机偶发）。

    契约只有两条，满足任一即可：
      · 回 `Connection: close`，让这条连接作废；
      · 或者把 body 读完丢掉，连接可以继续用。
    两者都没有 = 污染。
    """

    def test_跨站被拒后同连接的下一个请求仍正常(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", SETTINGS, body=PAYLOAD, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(PAYLOAD)),
                "Host": "192.168.43.18:8787",
                "Origin": "http://evil.example",
            })
            r = conn.getresponse()
            body = r.read()
            assert r.status == 403, body

            closing = (r.getheader("Connection") or "").lower() == "close"
            if not closing:
                # 没关连接，就得保证连接还能用一条完整的请求
                conn.request("GET", "/api/v1/laps",
                             headers={"Host": "192.168.43.18:8787"})
                r2 = conn.getresponse()
                b2 = r2.read()
                assert r2.status == 200, (
                    f"被拒请求的 body 残留在连接里，下一条请求被解析成 "
                    f"{r2.status}: {b2[:120]!r}")
        finally:
            conn.close()

    def test_host_被拒后同连接的下一个请求仍正常(self, port):
        """Host 校验 400 是同一条提前 return 路径，同样会漏读 body。"""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", SETTINGS, body=PAYLOAD, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(PAYLOAD)),
                "Host": "evil.example",
            })
            r = conn.getresponse()
            body = r.read()
            assert r.status == 400, body

            closing = (r.getheader("Connection") or "").lower() == "close"
            if not closing:
                conn.request("GET", "/api/v1/laps",
                             headers={"Host": "192.168.43.18:8787"})
                r2 = conn.getresponse()
                b2 = r2.read()
                assert r2.status == 200, (
                    f"Host 拒绝路径漏读 body，下一条请求被解析成 "
                    f"{r2.status}: {b2[:120]!r}")
        finally:
            conn.close()

    def test_连续多次被拒后连接依然可用(self, port):
        """同一连接上连发三条被拒请求，每条都要排空自己的 body。

        🔴 专门守 `_body_consumed` 忘了按请求重置的 bug：它是实例/类状态，
           keep-alive 下同一个 handler 复用，一旦不重置，**第二条**请求的
           body 就再也不会被排空 —— 单次测试完全看不出来。
        """
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            for i in range(3):
                payload = '{"value": %d}' % (i + 10)
                conn.request("POST", SETTINGS, body=payload, headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    "Host": "192.168.43.18:8787",
                    "Origin": "http://evil.example",
                })
                r = conn.getresponse()
                r.read()
                assert r.status == 403, f"第 {i + 1} 条"
                if (r.getheader("Connection") or "").lower() == "close":
                    pytest.skip("服务端选择关连接，本用例不适用")
            conn.request("GET", "/api/v1/laps",
                         headers={"Host": "192.168.43.18:8787"})
            r2 = conn.getresponse()
            b2 = r2.read()
            assert r2.status == 200, (
                f"多条被拒请求后连接被污染：{r2.status} {b2[:120]!r}")
        finally:
            conn.close()

    def test_被拒请求之后正常写仍能成功(self, port):
        """排空不能把下一条请求的 body 吃掉（排多 / 排少都是错）。"""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", SETTINGS, body=PAYLOAD, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(PAYLOAD)),
                "Host": "192.168.43.18:8787",
                "Origin": "http://evil.example",
            })
            r = conn.getresponse()
            r.read()
            assert r.status == 403
            if (r.getheader("Connection") or "").lower() == "close":
                pytest.skip("服务端选择关连接，本用例不适用")

            good = '{"value": 77}'
            conn.request("POST", SETTINGS, body=good, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(good)),
                "Host": "192.168.43.18:8787",
                "Origin": "http://192.168.43.18:8787",
            })
            r2 = conn.getresponse()
            b2 = r2.read()
            assert r2.status == 200, b2
            # 关键：这条请求必须读到自己 body 里的 77（排多会吃掉它）
            assert b"77.0" in b2.replace(b" ", b""), b2
        finally:
            conn.close()


class TestHostWhitelist:
    """DNS rebinding：恶意域名指到内网 IP 时，Host 会是那个域名。"""

    def test_陌生_host_被拒(self, port):
        r, body = _req(port, "GET", "/api/v1/laps", {"Host": "evil.example"})
        assert r.status == 400, body
        assert "Host" in body.decode("utf-8", "replace")

    def test_ip_字面量的_host_放行(self, port):
        r, _ = _req(port, "GET", "/api/v1/laps", {"Host": "192.168.43.18:8787"})
        assert r.status == 200

    def test_localhost_放行(self, port):
        r, _ = _req(port, "GET", "/api/v1/laps",
                    {"Host": "localhost:%d" % port})
        assert r.status == 200

    def test_allow_host_声明的域名放行(self, dash, tmp_path):
        """用 NAS 主机名访问的场景：必须能靠 --allow-host 放行。"""
        hist = tmp_path / "d2"
        hist.mkdir()

        class Handler(dash.DashboardHandler):
            pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.history_dir = str(hist)          # type: ignore[attr-defined]
        server.allow_hosts = ("nas.local",)     # type: ignore[attr-defined]
        dash.HUB = dash.TelemetryHub(hist / "status.json")
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            p = server.server_address[1]
            r, _ = _req(p, "GET", "/api/v1/laps", {"Host": "nas.local:8787"})
            assert r.status == 200
            # 同源写在域名下也要通（Origin 与 Host 都是 nas.local）
            r2, b2 = _req(p, "POST", SETTINGS, {
                "Content-Type": "application/json",
                "Content-Length": str(len(PAYLOAD)),
                "Host": "nas.local:8787",
                "Origin": "http://nas.local:8787",
            }, PAYLOAD)
            assert r2.status == 200, b2
        finally:
            server.shutdown()
            server.server_close()


class TestLogging:
    """日志：2xx 静默，4xx/5xx 必须留痕（原来全被 pass 吞掉）。"""

    def test_成功请求不刷日志(self, dash, capsys):
        h = dash.DashboardHandler.__new__(dash.DashboardHandler)
        h.client_address = ("127.0.0.1", 1234)
        h.log_message('"GET /api/state HTTP/1.1" 200 -')
        assert capsys.readouterr().err.strip() == ""

    def test_错误请求留日志(self, dash, capsys):
        h = dash.DashboardHandler.__new__(dash.DashboardHandler)
        h.client_address = ("192.168.43.9", 1234)
        h.log_message('"POST /api/v1/sessions/x/delete HTTP/1.1" 403 -')
        err = capsys.readouterr().err
        assert "403" in err
        assert "192.168.43.9" in err        # 来源 IP 必须能定位到设备
