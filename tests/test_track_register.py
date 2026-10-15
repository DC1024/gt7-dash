"""赛道库「一键标注」端点 POST /api/v1/tracks/register 集成测试。

这是「补齐 GT7 赛道库」的主入口：用户每跑一圈标注一次，
「算指纹 → 挂靠/新建赛道 → 写名」一次性完成并落库，同赛道所有场次一起生效。

📌 必须连真 HTTP 服务测：端点的请求解析 / 状态码 / 落库副作用只有起服务才验得出。
"""
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def srv(dash, tmp_path_factory):
    hist = tmp_path_factory.mktemp("data")

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


def _write_session(path, laps=3, frames_per_lap=1500, hz=60.0, base_t=1000.0,
                   coords=True):
    lines = [json.dumps({"session_id": path.stem, "circuit": "test",
                         "car": 1302, "powertrain": "fuel"}, ensure_ascii=False)]
    t = base_t
    for lap in range(1, laps + 1):
        for i in range(frames_per_lap):
            ph = i / frames_per_lap
            row = {"t": round(t, 4), "lap": lap,
                   "speed_kph": round(180.0 + 20 * ph, 2),
                   "throttle": round(max(0.0, 1 - 2 * ph), 3),
                   "brake": round(max(0.0, 2 * ph - 1), 3),
                   "g_force": [0.3, 0.1, 0.0]}
            if coords:
                row["car_x"] = 100.0 + 90.0 * (i / max(1, frames_per_lap - 1))
                row["car_z"] = 50.0 + 40.0 * (lap + ph)
            lines.append(json.dumps(row, ensure_ascii=False))
            t += 1 / hz
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _req(port, method, path, body=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    try:
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        b = r.read()
        return r.status, (json.loads(b.decode("utf-8", "replace")) if b else None)
    finally:
        conn.close()


class TestTrackRegister:
    def test_标注后赛道库与列表都生效(self, srv):
        port = srv.server_address[1]
        hist = Path(srv.history_dir)
        sess = _write_session(hist / "20261015_203104_test_aa11bb22.jsonl")

        st, body = _req(port, "POST", "/api/v1/tracks/register",
                        {"file": sess.name, "name": "Test Circuit X"})
        assert st == 200, body
        assert body["ok"] and body["name"] == "Test Circuit X"
        tid = body["track_id"]

        # 赛道库出现该名，且不再是「按圈长猜」
        _, tracks = _req(port, "GET", "/api/v1/tracks")
        entry = next(t for t in tracks["tracks"] if t["id"] == tid)
        assert entry["name"] == "Test Circuit X"
        assert entry["auto_named"] is False
        assert entry["verified"] is True

        # 列表该场带名（不再显示占位「未知/未命名」）
        _, sl = _req(port, "GET", "/api/v1/sessions?limit=50")
        row = next(r for r in sl["sessions"] if r["file"] == sess.name)
        assert row["track_name"] == "Test Circuit X"

    def test_同几何的第二场自动挂靠同一赛道(self, srv):
        """两个几何相同的场次，标注第一个后，第二个应匹配到同一 track_id。"""
        port = srv.server_address[1]
        hist = Path(srv.history_dir)
        a = _write_session(hist / "20261015_210000_test_cc22dd33.jsonl")
        b = _write_session(hist / "20261015_210500_test_ee44ff55.jsonl")

        _, ba = _req(port, "POST", "/api/v1/tracks/register",
                     {"file": a.name, "name": "Same Geometry Circuit"})
        tid_a = ba["track_id"]
        # 第二场几何相同 → session_track 应匹配到同一库条目（distance≈0）
        _, bb = _req(port, "POST", "/api/v1/tracks/register",
                     {"file": b.name, "name": "Same Geometry Circuit"})
        assert bb["track_id"] == tid_a, "同几何应挂靠同一赛道"

    def test_改名幂等更新(self, srv):
        port = srv.server_address[1]
        hist = Path(srv.history_dir)
        sess = _write_session(hist / "20261015_220000_test_11223344.jsonl")
        _req(port, "POST", "/api/v1/tracks/register",
             {"file": sess.name, "name": "临时名"})
        _, b2 = _req(port, "POST", "/api/v1/tracks/register",
                     {"file": sess.name, "name": "正式名 Circuit"})
        assert b2["name"] == "正式名 Circuit"
        _, tracks = _req(port, "GET", "/api/v1/tracks")
        e = next(t for t in tracks["tracks"] if t["id"] == b2["track_id"])
        assert e["name"] == "正式名 Circuit"

    def test_缺参数_400(self, srv):
        port = srv.server_address[1]
        st, _ = _req(port, "POST", "/api/v1/tracks/register",
                     {"file": "x.jsonl"})  # 缺 name
        assert st == 400
        st, _ = _req(port, "POST", "/api/v1/tracks/register",
                     {"name": "Only Name"})  # 缺 file
        assert st == 400

    def test_文件不存在_404(self, srv):
        port = srv.server_address[1]
        st, _ = _req(port, "POST", "/api/v1/tracks/register",
                     {"file": "nope.jsonl.gz", "name": "X"})
        assert st == 404
