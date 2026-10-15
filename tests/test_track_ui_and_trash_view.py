"""赛道地图管理页与回收站查看页回归测试。"""
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def srv(dash, tmp_path_factory):
    hist = tmp_path_factory.mktemp("data-track-ui")
    (hist / "_trash").mkdir()

    class Handler(dash.DashboardHandler):
        pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.history_dir = str(hist)  # type: ignore[attr-defined]
    dash.HUB = dash.TelemetryHub(hist / "status.json")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _req(port, method, path, body=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    try:
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        b = r.read()
        return r.status, b.decode("utf-8", "replace")
    finally:
        conn.close()


def _write_session(path, laps=2, frames_per_lap=1500, hz=60.0):
    lines = [json.dumps({"session_id": path.stem, "circuit": None,
                         "car": 1302, "powertrain": "fuel"})]
    t = 1000.0
    for lap in range(1, laps + 1):
        for i in range(frames_per_lap):
            ph = i / frames_per_lap
            row = {"t": round(t, 4), "lap": lap,
                   "speed_kph": 180.0 + 20 * ph,
                   "rpm": 6000, "throttle": 0.5, "brake": 0.0,
                   "car_x": 100.0 + 90.0 * __import__("math").cos(ph * 6.283),
                   "car_z": 50.0 + 90.0 * __import__("math").sin(ph * 6.283),
                   "g_force": [0.3, 0.1, 0.0]}
            lines.append(json.dumps(row))
            t += 1 / hz
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestTrackPageAndConfirm:
    def test_仪表盘导航含赛道地图链接(self, dash):
        assert '<a href="/tracks">赛道地图</a>' in dash.build_page()

    def test_赛道页面显示识别形状及可编辑状态(self, srv, dash):
        hist = Path(srv.history_dir)
        (hist / "tracks.json").write_text(json.dumps({
            "next_id": 2,
            "tracks": [{"id": 1, "name": "测试赛道", "desc": [[0, 0], [0.5, 1], [1, 0]],
                        "ref_len_m": 1000, "turns_cw": True, "auto_named": True}],
            "sessions": {"sample.jsonl": {"track_id": 1}},
        }), encoding="utf-8")
        html = dash.build_tracks_page(hist)
        assert 'canvas class="trackDesc"' in html
        assert "测试赛道" in html
        assert "自动识别·待确认" in html
        assert "trackSave(this)" in html
        assert "trackConfirm(this)" in html
        assert "请告诉我" in html
        assert "trackFreeze" not in html
        assert "/api/v1/tracks/freeze" not in html

    def test_赛道页面真的调用绘制函数(self, srv, dash):
        # 回归：2026-10-15 线上页面只有 drawAllTracks 定义、没有调用，
        # 26 张 canvas 全空白。定义在共享块里，调用必须单独断言。
        # （空赛道库走无脚本的空态分支，所以这里要放一条赛道。）
        hist = Path(srv.history_dir)
        (hist / "tracks.json").write_text(json.dumps({
            "next_id": 2,
            "tracks": [{"id": 1, "name": "形状回归", "desc": [[0, 0], [1, 1]],
                        "ref_len_m": 900, "turns_cw": True}],
            "sessions": {},
        }), encoding="utf-8")
        html = dash.build_tracks_page(hist)
        pos_def = html.find("function drawAllTracks")
        pos_call = html.rfind("drawAllTracks();")
        assert pos_def != -1 and pos_call > pos_def

    def test_confirm_标记已确认(self, srv):
        hist = Path(srv.history_dir)
        (hist / "tracks.json").write_text(json.dumps({
            "next_id": 2,
            "tracks": [{"id": 1, "name": "名字正确", "desc": [], "ref_len_m": 1000,
                        "auto_named": True}],
            "sessions": {},
        }), encoding="utf-8")
        st, body = _req(srv.server_address[1], "POST", "/api/v1/tracks/1/confirm", {})
        assert st == 200 and json.loads(body)["verified"] is True
        saved = json.loads((hist / "tracks.json").read_text(encoding="utf-8"))
        assert saved["tracks"][0]["verified"] is True
        assert saved["tracks"][0]["auto_named"] is False

    def test_no_public_freeze_route(self, srv):
        status, body = _req(srv.server_address[1], "POST", "/api/v1/tracks/freeze", {})
        assert status == 404
        assert "tracks_seed.json" not in body


class TestTrashView:
    def test_回收站详情页可查看且提供恢复删除(self, srv, dash):
        hist = Path(srv.history_dir)
        f = _write_session(hist / "_trash" / "trash-check.jsonl")
        page = dash.build_trash_view_page(hist, f.name)
        assert "回收站" in page
        assert "恢复到历史" in page
        assert "彻底删除" in page
        assert "canvas class=\"trackDesc\"" in page
        assert f.name in page

    def test_回收站清单每项有查看链接(self, dash):
        assert "/trash-view?file=" in dash._SESSIONS_PAGE_JS
        assert "encodeURIComponent(t.name)" in dash._SESSIONS_PAGE_JS

    def test_路径防护不允许查看回收站外文件(self, srv, dash):
        hist = Path(srv.history_dir)
        outside = hist / "outside.jsonl"
        _write_session(outside)
        page = dash.build_trash_view_page(hist, "../outside.jsonl")
        assert "文件不存在或已清理" in page
        assert "恢复到历史" not in page

    def test_回收站查看路由在线返回页面(self, srv):
        status, body = _req(srv.server_address[1], "GET", "/trash-view?file=missing.jsonl")
        assert status == 200
        assert "文件不存在或已清理" in body

    def test_tracks_page路由在线返回页面(self, srv):
        status, body = _req(srv.server_address[1], "GET", "/tracks")
        assert status == 200
        assert "赛道地图" in body
