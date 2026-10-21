# -*- coding: utf-8 -*-
"""场次名 / 文件名的 HTML 注入（P1-1）。

威胁模型不是"远程攻击者"，而是**持久化 XSS**：
用户给某个场次改个名字（或者场次文件名里带了特殊字符），这段文本就被写进
`sessions_meta.json`，此后**每次打开 /sessions 都会执行**。一次注入，
永久生效 —— 连重启服务都没用，因为脏数据已经落盘了。

三处注入面，必须都堵：
  1. `display_name` / `circuit` 直接插进 `<a>` 的文本节点；
  2. `s['file']` 插进 `href="/session?file=..."` 属性；
  3. `s['file']` 插进 `onclick="sessRen('...')"` 这类**内联 JS 字符串字面量**。

第 3 类最容易被忽略：HTML 转义管不了 JS 上下文，单引号和反斜杠才是那里的元字符。
"""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

PAYLOAD_TEXT = '<img src=x onerror="window.__pwned=1">'


@pytest.fixture(scope="module")
def srv(dash, tmp_path_factory):
    hist = tmp_path_factory.mktemp("data_xss")

    class Handler(dash.DashboardHandler):
        pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.history_dir = str(hist)          # type: ignore[attr-defined]
    server.allow_hosts = ()                  # type: ignore[attr-defined]
    dash.HUB = dash.TelemetryHub(hist / "status.json")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _make_session(hist: Path, filename: str) -> Path:
    """造一个能被 list_sessions 认出来的最小场次文件。

    🔴 圈长必须 ≥20s：`_is_anomalous()` 把"唯一圈 <20s"判为菜单/停车场残片，
       `list_sessions()` 会直接把它移进回收站 —— 于是这一行根本不渲染，
       XSS 断言就会以"页面里没有 &lt;img"的形式失败，看起来像修复没生效。
    """
    p = hist / filename
    rows = []
    # 一条 24 秒的完成圈（1200 帧 @ 50 Hz）
    for i in range(1200):
        t = round(i * 0.02, 3)
        rows.append({"t": t, "lap": 1, "speed": 100.0,
                     "car_x": float(i), "car_z": 0.0,
                     "on_track": True, "lap_time": t,
                     "car_id": 1, "rpm": 5000.0, "gear": 3,
                     "throttle": 100, "brake": 0,
                     "wheel_speed_fl": 30.0, "wheel_speed_fr": 30.0,
                     "wheel_speed_rl": 30.0, "wheel_speed_rr": 30.0})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


class TestDisplayNameEscaped:
    def test_恶意场次名被转义(self, dash, srv, tmp_path):
        """改名成 `<img onerror>` 后，页面里必须是 &lt;img 而不是 <img。

        🔴 走真实的改名接口（POST /api/v1/sessions/<file>/rename），
           而不是手写 meta：meta 里的键是 `custom_name`（输入），
           `display_name` 是 list_sessions 算出来的**输出**字段 —— 写成
           display_name 会被静默忽略，测试就变成"永远绿的空转"。
        """
        hist = Path(srv.history_dir)
        _make_session(hist, "xss-test.jsonl")
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                          timeout=10)
        try:
            # 字段名是 value（不是 name）；服务端会截断到 60 字符，
            # 这里的 payload 只有 40 出头，不会被截。
            body = json.dumps({"value": PAYLOAD_TEXT})
            assert len(PAYLOAD_TEXT) <= 60, "payload 会被截断，换一个短点的"
            conn.request("POST", "/api/v1/sessions/xss-test.jsonl/rename",
                         body=body, headers={
                             "Content-Type": "application/json",
                             "Content-Length": str(len(body.encode())),
                             "Host": "127.0.0.1:%d" % srv.server_address[1],
                         })
            r = conn.getresponse()
            assert r.status == 200, r.read()
            r.read()
        finally:
            conn.close()

        # 确认脏名字真的落盘了（否则下面的断言等于没测）
        saved = dash.load_session_meta(hist).get("xss-test.jsonl", {})
        assert saved.get("custom_name") == PAYLOAD_TEXT, saved

        status, html = _get(srv.server_address[1], "/sessions")
        assert status == 200
        assert PAYLOAD_TEXT not in html, "未转义的名字被原样输出 → 持久化 XSS"
        assert "&lt;img" in html, "期望被 HTML 转义"

    def test_恶意赛道名被转义(self, dash, srv):
        """赛道名同样进页面（多列重复渲染），别只转义 display_name。

        用**独立场次文件**：同一条场次被上一个用例改过名，
        而 list_sessions 会缓存最快圈（按 mtime+size 命中），互相干扰。
        """
        hist = Path(srv.history_dir)
        _make_session(hist, "track-xss.jsonl")
        # 赛道名来自识别库（tracks.json），不是 meta —— 直接写库更贴近真实来源
        lib = dash.load_tracks(hist)
        lib["tracks"].append({"id": "t-evil", "name": PAYLOAD_TEXT,
                              "ref_len_m": 3000.0})
        lib["sessions"]["track-xss.jsonl"] = {"track_id": "t-evil"}
        dash.save_tracks(hist, lib)

        _, html = _get(srv.server_address[1], "/sessions")
        assert PAYLOAD_TEXT not in html
        assert "&lt;img" in html


class TestFileNameEscaped:
    """文件名来自磁盘（recorder 生成），但一样不能无条件信任。"""

    def test_href_里的文件名被_url_编码(self, dash, srv):
        hist = Path(srv.history_dir)
        _make_session(hist, "a b&c.jsonl")
        _, html = _get(srv.server_address[1], "/sessions")
        # 空格/& 必须被编码，否则属性会被截断或实体化
        assert 'href="/session?file=a b&c.jsonl"' not in html
        assert "file=a%20b%26c.jsonl" in html or "file=a+b%26c.jsonl" in html

    def test_onclick_里的文件名不破坏_js_字面量(self, dash, srv):
        """引号是内联 JS 的元字符，HTML 转义救不了它。

        🔴 断言必须精确：我第一版写成「`sessRen('q\\'uote.jsonl')` 或 `&#x27;`
           或另一种写法」三选一，结果**撤回修复后它照样绿** —— 因为 `&#x27;`
           这个分支在页面里永远存在（别处有转义的名字）。宽松断言等于没测。
        """
        hist = Path(srv.history_dir)
        _make_session(hist, "q'uote.jsonl")
        _, html = _get(srv.server_address[1], "/sessions")
        # 未修复时会渲染成 sessRen(&quot;q'uote.jsonl&quot;) ——
        # 属性里的 &quot; 解码后是 "，而 JS 里那个裸 ' 会提前结束字符串？
        # 不会（外层是双引号），但 `q'uote` 里的 ' 在单引号写法下会崩。
        # 真正确认的是：文件名里的 ' 被转成 \\' ，且整体用双引号包裹。
        assert "sessRen(&quot;q'uote.jsonl&quot;)" not in html, \
            "文件名里的单引号未转义，内联 JS 会被提前截断"
        assert 'sessRen(&quot;q\\\'uote.jsonl&quot;)' in html, \
            "期望 ' 被转义成 \\' 且用双引号包裹"
