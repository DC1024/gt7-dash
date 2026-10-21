# -*- coding: utf-8 -*-
"""F1：公开 API v1 契约测试。

公开 API 是对外承诺：**只能加字段，不能改名/改单位**。这里用一个
确定性场次（3 个完整圈 + 1 个离场圈）把关键端点的「必填字段 + 类型 +
单位」钉死；以后谁把 `speed_kph` 改成 m/s、把 `laps[].time` 改成毫秒，
或者把 `best_lap` 从对象改成数字，这里立刻红。

不追求「响应完全等于快照」——那会让正常的新增字段也失败。只断言
**必须存在且类型正确**的字段，以及几个能验证单位的具体数值。
"""
import http.client
import json
import math
import threading
from http.server import ThreadingHTTPServer

import pytest


def _lap(lap: int, n: int, t0: float, last_lap_ms: int | None = None,
         speed_scale: float = 1.0, radius: float = 100.0) -> list[dict]:
    dt = 0.02
    chord = 2 * math.pi * radius / n
    v_ms = chord / dt * speed_scale
    out = []
    for i in range(n):
        ang = 2 * math.pi * i / n
        fr = {
            "t": t0 + i * dt, "lap": lap, "speed_kph": v_ms * 3.6,
            "rpm": 5000.0, "throttle": 0.5, "brake": 0.0, "gear": 4,
            "car_x": radius * math.cos(ang), "car_z": radius * math.sin(ang),
            "car_code": 3459, "gas_level": 50.0, "gas_capacity": 60.0,
            "quali_pos": 3, "num_cars": 20, "tyre_temp": [80.0] * 4,
            "susp_height": [0.05] * 4, "wheel_rads": [50.0] * 4,
            "g_force": [0.1, 0.2, 0.0], "has_coords": True, "layout": "A",
        }
        if i == 0 and last_lap_ms is not None:
            fr["last_lap_ms"] = last_lap_ms
        out.append(fr)
    return out


def _write_session(path, frames):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"session_id": "x", "started_at": 1000.0}) + "\n")
        for fr in frames:
            f.write(json.dumps(fr, separators=(",", ":")) + "\n")


@pytest.fixture(scope="module")
def api(dash, tmp_path_factory):
    hist = tmp_path_factory.mktemp("api-contract")
    (hist / "_trash").mkdir()
    (hist / "settings.json").write_text(
        json.dumps({"archive_after_days": 0, "trash_retention_days": 30}),
        encoding="utf-8")

    name = "20261022_010000_TestTrack.jsonl"
    t = 1000.0
    frames = _lap(1, 1500, t)
    t += 30.0
    frames += _lap(2, 1500, t, last_lap_ms=24000)
    t += 30.0
    frames += _lap(3, 1500, t, last_lap_ms=23500)
    t += 30.0
    frames += _lap(4, 5, t, last_lap_ms=22000, speed_scale=0.0)
    _write_session(hist / name, frames)

    # schema 2 状态文件：/api/v1/live 用
    now = 1792599000.0
    hist_frames = [
        {"q": i, "t": now - (5 - i) * 0.02, "s": 100.0, "r": 6000, "g": 4,
         "th": 0.5, "bk": 0.0, "lap": 1, "wt": [80] * 4, "wf": [50] * 4,
         "gf": [0.1, 0.2, 0.0]} for i in range(1, 6)
    ]
    (hist / "status.json").write_text(json.dumps({
        "schema": 2, "seq": 5, "t": now, "last_rx_t": now,
        "frame": {"t": now, "speed_kph": 100.0, "rpm": 6000, "gear": 4,
                  "throttle": 0.5, "brake": 0.0, "lap": 1, "car_code": 3459,
                  "car_x": 1.0, "car_y": 0.0, "car_z": 2.0,
                  "velocity": [27.8, 0.0, 0.0], "g_force": [0.1, 0.2, 0.0],
                  "gas_level": 50.0, "gas_capacity": 60.0, "has_coords": True,
                  "quali_pos": 3, "num_cars": 20},
        "history": hist_frames, "full": hist_frames,
        "session_start": now - 10, "frames_total": 5,
        "lap_times": [[1, 24000]], "lap_fuel": [], "recording": True,
        "powertrain": "fuel", "max_energy_recovery": 0.0,
        "session_max_speed": 120.0,
    }), encoding="utf-8")

    dash.HUB = dash.TelemetryHub(hist / "status.json")

    class Handler(dash.DashboardHandler):
        pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.history_dir = str(hist)   # type: ignore[attr-defined]
    srv.allow_hosts = ()          # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", name
    finally:
        srv.shutdown()
        srv.server_close()


def _get(base, path):
    from urllib.parse import urlparse
    u = urlparse(base + path)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    try:
        conn.request("GET", u.path + (("?" + u.query) if u.query else ""))
        r = conn.getresponse()
        body = r.read()
        ctype = r.getheader("Content-Type") or ""
        return r.status, ctype, body, r.getheaders()
    finally:
        conn.close()


def _json(base, path):
    code, ctype, body, _ = _get(base, path)
    assert code == 200, (path, code, body[:200])
    assert "application/json" in ctype, (path, ctype)
    return json.loads(body)


def test_sessions_list_contract(api):
    base, name = api
    d = _json(base, "/api/v1/sessions")
    assert d["meta"]["api_version"] == 1
    assert isinstance(d["data_dir"], str)
    rows = d["sessions"]
    assert isinstance(rows, list) and rows
    row = next(r for r in rows if r["file"] == name)
    # 必填字段 + 类型（单位：秒 / km / KB）
    assert isinstance(row["car_code"], int) and row["car_code"] == 3459
    assert isinstance(row["best_lap_s"], (int, float))
    assert row["best_lap_s"] == pytest.approx(22.0)
    for k in ("timestamp", "time_of_day", "circuit", "modified", "time_str",
              "car_name", "car_maker", "car_group", "display_name",
              "track_name"):
        assert isinstance(row[k], str), (k, type(row[k]))
    for k in ("live", "archived", "favorite", "anomalous"):
        assert isinstance(row[k], bool), (k, type(row[k]))


def test_session_detail_contract(api):
    base, name = api
    d = _json(base, f"/api/v1/sessions/{name}")
    assert d["meta"]["api_version"] == 1
    assert isinstance(d["frame_count"], int) and d["frame_count"] == 4505
    assert isinstance(d["duration"], (int, float)) and d["duration"] > 0
    assert isinstance(d["max_speed"], (int, float))     # km/h
    assert isinstance(d["max_rpm"], (int, float))
    assert d["car_code"] == 3459
    assert isinstance(d["layouts"], dict)
    laps = d["laps"]
    assert isinstance(laps, list) and len(laps) >= 3
    best = d["best_lap"]
    assert best["lap"] == 3
    assert best["time"] == pytest.approx(22.0)          # 秒，不是毫秒
    assert best["time_source"] == "official"
    # 圈长单位是米（圆周长 ≈ 628m）
    assert best["dist_m"] == pytest.approx(2 * math.pi * 100, rel=0.02)


def test_live_contract(api):
    base, _ = api
    d = _json(base, "/api/v1/live?frames=120")
    assert d["meta"]["api_version"] == 1
    assert isinstance(d["connected"], bool)
    car = d["car"]
    assert isinstance(car["speed_kph"], (int, float)) and car["speed_kph"] == 100.0
    assert car["car_code"] == 3459
    assert isinstance(car["gear"], int)
    assert isinstance(car["g_force"], dict)
    for k in ("longitudinal", "lateral", "magnitude"):
        assert isinstance(car["g_force"][k], (int, float))
    sess = d["session"]
    assert isinstance(sess["frames"], int)
    assert isinstance(sess["lap_times"], list)
    frames = d["history"]
    assert isinstance(frames, list) and 0 < len(frames) <= 120
    for k in ("t", "speed_kph", "rpm", "gear", "throttle", "brake",
              "lap", "g_force", "tyre_temp_c"):
        assert k in frames[0], k


def test_laps_contract(api):
    base, _ = api
    d = _json(base, "/api/v1/laps")
    assert d["meta"]["api_version"] == 1
    assert d["completed_laps"] == 1
    assert d["best_lap_ms"] == 24000          # 毫秒（与 lap_times 一致）
    assert d["lap_times"] == [[1, 24000]]


def test_series_contract(api):
    base, name = api
    d = _json(base, f"/api/v1/sessions/{name}/series?max_points=200")
    assert isinstance(d["cols"], list) and d["cols"]
    assert isinstance(d["rows"], list) and d["rows"]
    assert all(len(r) == len(d["cols"]) for r in d["rows"])
    assert d["total_frames"] == 4505
    assert isinstance(d["scope_frames"], int)
    assert isinstance(d["step"], int) and d["step"] >= 1
    assert isinstance(d["laps"], list)


def test_sectors_contract(api):
    base, name = api
    d = _json(base, f"/api/v1/sessions/{name}/sectors")
    assert d["n_sectors"] == 4
    assert d["actual_best_lap"] == 3
    assert d["actual_best_s"] == pytest.approx(22.0)   # 秒
    assert isinstance(d["reliable"], bool)
    assert isinstance(d["laps"], list)
    row = next(r for r in d["laps"] if r["lap"] == 3)
    assert len(row["sectors"]) == 4
    assert row["total_s"] == pytest.approx(22.0)


def test_compare_contract(api):
    base, name = api
    d = _json(base, f"/api/v1/sessions/{name}/compare")
    assert d["ref_lap"] == 3
    assert isinstance(d["lap_summary"], list)
    assert isinstance(d["time_diff"], dict)
    assert "step" in d["time_diff"] and "grid" in d["time_diff"]
    assert isinstance(d["race_line"]["segments"], list)


def test_pitstops_contract(api):
    base, name = api
    d = _json(base, f"/api/v1/sessions/{name}/pitstops")
    assert isinstance(d["available"], bool)
    for k in ("stints", "positions", "pos_events", "laps_list"):
        assert isinstance(d[k], list), k
    assert isinstance(d["lap_durs"], dict)


def test_reference_data_contract(api):
    base, _ = api
    t = _json(base, "/api/v1/tyres")
    assert t["meta"]["api_version"] == 1
    assert isinstance(t["life"], list) and t["life"]
    assert isinstance(t["wet"], list)
    assert isinstance(t["source"], str)

    s = _json(base, "/api/v1/settings")
    assert isinstance(s["trash_retention_days"], (int, float))
    assert isinstance(s["session_name_template"], str)
    assert isinstance(s["trash"], list)


def test_error_semantics(api):
    base, name = api
    code, _, body, _ = _get(base, "/api/v1/sessions/nope.jsonl")
    assert code == 404 and "error" in json.loads(body)

    code, _, body, _ = _get(base, f"/api/v1/sessions/{name}/series?cols=bogus")
    assert code == 400
    err = json.loads(body)
    assert "error" in err and isinstance(err.get("allowed"), list)

    code, _, _, _ = _get(base, "/api/v1/sessions/../../etc/passwd")
    assert code == 404


def test_cors_contract(api):
    """读接口带通配 CORS；写接口不带（防跨站写）。"""
    base, _ = api
    _, _, _, headers = _get(base, "/api/v1/sessions")
    h = {k.lower(): v for k, v in headers}
    assert h.get("access-control-allow-origin") == "*"

    from urllib.parse import urlparse
    u = urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    try:
        conn.request("POST", "/api/v1/settings/trash-retention",
                     body=json.dumps({"value": 7}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        assert r.status == 200
        assert r.getheader("Access-Control-Allow-Origin") is None
    finally:
        conn.close()
