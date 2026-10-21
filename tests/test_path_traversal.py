# -*- coding: utf-8 -*-
"""场次路径穿越（P1-2）。

`/session?file=` 这类入口直接把用户给的字符串拼进 `hist / name` 再 resolve。
`resolve()` 能挡掉 `../`（跑出 hist 之后落在别处），但**挡不住兄弟目录**：

    hist   = /opt/gt7/data
    target = /opt/gt7/data_evil/leak.jsonl
    str(target).startswith(str(hist))  →  True   ← 前缀法在这里失效

真正可靠的边界判断是 `Path.relative_to()`：它按**路径分量**比较，
`/opt/gt7/data_evil` 相对 `/opt/gt7/data` 会抛 ValueError。
"""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture()
def srv(dash, tmp_path):
    # data/ 与 data_evil/ 是兄弟目录 —— 这正是前缀法挡不住的情形
    hist = tmp_path / "data"
    hist.mkdir()
    evil = tmp_path / "data_evil"
    evil.mkdir()
    _make_session(evil / "leak.jsonl")
    _make_session(hist / "good.jsonl")

    class Handler(dash.DashboardHandler):
        pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.history_dir = str(hist)          # type: ignore[attr-defined]
    server.allow_hosts = ()                  # type: ignore[attr-defined]
    dash.HUB = dash.TelemetryHub(hist / "status.json")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, hist, evil
    finally:
        server.shutdown()
        server.server_close()


def _make_session(p: Path, lap: int = 1):
    """用沿用的 synth_lap 造真实形态的场次（别手搓字段名）。

    🔴 字段名是 `speed_kph` 不是 `speed`：手搓的话分析器会 500
       （`{"error": "'speed_kph'"}`），让人误以为"路径校验没生效"。
    """
    from synth_lap import synth_lap
    frames = synth_lap(radius_m=120.0, base_kph=180.0, hz=60, lap=lap)
    p.write_text("\n".join(json.dumps(f) for f in frames) + "\n",
                 encoding="utf-8")


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        conn.close()


# 归一化后的「兄弟目录」穿越：resolve 之后落在 hist 之外，但前缀与 hist 相同
SIBLING = "..%2Fdata_evil%2Fleak.jsonl"
BAD_NAMES = [
    "..%2Fdata_evil%2Fleak.jsonl",       # 兄弟目录（前缀法失效的经典形态）
    "..%2F%2Fetc%2Fpasswd",              # 绝对路径逃逸
    "%2Fetc%2Fpasswd",                   # 绝对路径
    "..%2F..%2Fsecret.jsonl",            # 多级上跳
    "good.jsonl%00.png",                 # 空字节截断（历史遗留攻击面）
]


class TestSiblingDirectoryEscape:
    """/session?file= 三个入口都不能读到 hist 之外的文件。"""

    @pytest.mark.parametrize("bad", BAD_NAMES)
    def test_session_page_拒绝兄弟目录(self, srv, bad):
        server, _, _ = srv
        status, body = _get(server.server_address[1], f"/session?file={bad}")
        assert status == 404, f"{bad} 被放行了：{body[:200]}"
        # 绝不能把内容吐出来
        assert "lap_time" not in body and '"t":' not in body

    @pytest.mark.parametrize("bad", BAD_NAMES)
    def test_analyze_拒绝兄弟目录(self, srv, bad):
        server, _, _ = srv
        status, body = _get(server.server_address[1], f"/analyze?file={bad}")
        assert status == 404, f"{bad} 被放行了：{body[:200]}"

    @pytest.mark.parametrize("bad", BAD_NAMES)
    def test_csv_拒绝兄弟目录(self, srv, bad):
        server, _, _ = srv
        status, body = _get(server.server_address[1], f"/session/csv?file={bad}")
        assert status == 404, f"{bad} 被放行了：{body[:200]}"


class TestSiblingDirectoryEscapeApi:
    """/api/v1/sessions/<file>/... 走的是路径段，同样要挡。"""

    @pytest.mark.parametrize("bad", [
        "..%2Fdata_evil%2Fleak.jsonl",
        "%2Fetc%2Fpasswd",
        "..%2F..%2Fsecret.jsonl",
    ])
    def test_api_拒绝兄弟目录(self, srv, bad):
        server, _, _ = srv
        for suffix in ("profile", "laps", "events"):
            status, body = _get(
                server.server_address[1],
                f"/api/v1/sessions/{bad}/{suffix}")
            assert status == 404, f"{bad}/{suffix} 被放行：{body[:160]}"


class TestLegitStillWorks:
    """收紧边界不能误伤正常用法。"""

    def test_正常场次仍可访问(self, srv):
        server, _, _ = srv
        status, body = _get(server.server_address[1],
                            "/session?file=good.jsonl")
        assert status == 200, body[:300]

    def test_正常场次的_api_仍可用(self, srv):
        server, _, _ = srv
        status, body = _get(server.server_address[1],
                            "/api/v1/sessions/good.jsonl/profile")
        assert status == 200, body[:300]

    def test_归档后的_gz_仍能按原名字访问(self, srv):
        """resolve_session 的 .gz 兜底不能被挡住（书签/收藏存的是未压缩名）。"""
        server, hist, _ = srv
        src = hist / "good.jsonl"
        raw = src.read_bytes()
        import gzip
        (hist / "good.jsonl.gz").write_bytes(gzip.compress(raw))
        src.unlink()
        status, body = _get(server.server_address[1],
                            "/session?file=good.jsonl")
        assert status == 200, body[:300]


class TestSafeSessionHelper:
    """直接测 _safe_session 本身（边界判断的唯一实现）。"""

    def test_兄弟目录返回_none(self, dash, tmp_path):
        hist = tmp_path / "data"
        hist.mkdir()
        evil = tmp_path / "data_evil"
        evil.mkdir()
        _make_session(evil / "leak.jsonl")
        assert dash._safe_session(hist, "../data_evil/leak.jsonl") is None

    def test_上跳一律拒绝(self, dash, tmp_path):
        """`../../ok.jsonl` 即使最终落回 hist 内也拒绝。

        🔴 我第一版写的是"应被剥离成 ok.jsonl 然后接受"，那是错的：
           剥离目录会让**符号链接目录逃逸**在到达 relative_to 之前被抹平，
           relative_to 就成了永不失败的死代码（实测：换成 startswith 照样全绿）。
           现在 `_safe_session` 不做剥离，上跳就是上跳 —— 拒绝。
           剥离的责任在路径段入口（`Path(seg[4]).name`）。
        """
        hist = tmp_path / "data"
        hist.mkdir()
        _make_session(hist / "ok.jsonl")
        assert dash._safe_session(hist, "../../ok.jsonl") is None

    def test_裸文件名正常接受(self, dash, tmp_path):
        hist = tmp_path / "data"
        hist.mkdir()
        _make_session(hist / "ok.jsonl")
        got = dash._safe_session(hist, "ok.jsonl")
        assert got is not None and got.name == "ok.jsonl"

    def test_非场次后缀拒绝(self, dash, tmp_path):
        hist = tmp_path / "data"
        hist.mkdir()
        (hist / "passwd").write_text("x")
        assert dash._safe_session(hist, "passwd") is None

    def test_不存在返回_none(self, dash, tmp_path):
        hist = tmp_path / "data"
        hist.mkdir()
        assert dash._safe_session(hist, "nope.jsonl") is None


class TestSymlinkEscape:
    """符号链接：**唯一能真正击穿前缀法的形态**。

    🔴 这一组是我第一版测试漏掉的。我原来只打 `../data_evil/x.jsonl`，
       但 `_safe_session` 里 `Path(name).name` 会先把它剥成 `x.jsonl`，
       于是穿越在到达边界判断之前就被"意外"挡住了 —— 把 `relative_to`
       换回 `startswith` 测试依然全绿，等于没测到真东西。

       真正的绕过是：**hist 内部**放一个指向外面的符号链接目录，
       请求 `linkdir/leak.jsonl`。这时：
         · 剥离目录后仍是 hist 内的路径（前缀判断为 True）；
         · 但 resolve() 之后落在 hist 之外 → `relative_to` 抛 ValueError。

       Windows 上创建目录符号链接需要开发者模式/管理员，没有就跳过。
    """

    @pytest.fixture()
    def linked(self, tmp_path):
        hist = tmp_path / "data"
        hist.mkdir()
        evil = tmp_path / "data_evil"
        evil.mkdir()
        _make_session(evil / "leak.jsonl")
        link = hist / "linkdir"
        try:
            link.symlink_to(evil, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            pytest.skip(f"本机无法创建目录符号链接：{e}")
        return hist, evil

    def test_符号链接目录不能逃出_hist(self, dash, linked):
        hist, _ = linked
        assert dash._safe_session(hist, "linkdir/leak.jsonl") is None

    def test_符号链接目录_http_也拒绝(self, srv_hist_linked):
        server, _ = srv_hist_linked
        status, body = _get(server.server_address[1],
                            "/session?file=linkdir%2Fleak.jsonl")
        assert status == 404, f"符号链接逃逸被放行：{body[:200]}"


@pytest.fixture()
def srv_hist_linked(dash, tmp_path):
    """一个 hist 内含符号链接目录的服务器。"""
    hist = tmp_path / "data"
    hist.mkdir()
    evil = tmp_path / "data_evil"
    evil.mkdir()
    _make_session(evil / "leak.jsonl")
    _make_session(hist / "good.jsonl")
    link = hist / "linkdir"
    try:
        link.symlink_to(evil, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"本机无法创建目录符号链接：{e}")

    class Handler(dash.DashboardHandler):
        pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.history_dir = str(hist)          # type: ignore[attr-defined]
    server.allow_hosts = ()                  # type: ignore[attr-defined]
    dash.HUB = dash.TelemetryHub(hist / "status.json")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, hist
    finally:
        server.shutdown()
        server.server_close()
