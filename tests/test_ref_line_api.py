# -*- coding: utf-8 -*-
"""E2 参考走线的端到端契约（生成 → 落盘 → 读回）。

三个必须守住的行为：
  · GET 只读缓存，**绝不**在 GET 里现场生成（读多场 70MB 要几十秒，
    浏览器会以为服务挂了）；
  · 生成失败要给出可执行的原因（"这条赛道没有挂靠场次"而不是 500）；
  · 落盘后重启进程仍能读到（写进 ref_lines.json，不是只放内存）。
"""
import http.client
import json
import math
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _circle_lap(lap: int, n: int, t0: float, radius: float = 100.0,
                last_lap_ms: int | None = None) -> list[dict]:
    dt = 0.02
    out = []
    for i in range(n):
        ang = 2 * math.pi * i / n
        fr = {
            "t": t0 + i * dt, "lap": lap,
            "speed_kph": (2 * math.pi * radius / n) / dt * 3.6,
            "rpm": 6000.0, "throttle": 0.6, "brake": 0.0, "gear": 4,
            "car_x": radius * math.cos(ang), "car_z": radius * math.sin(ang),
            "car_code": 3459, "has_coords": True,
            "tyre_temp": [80.0] * 4, "susp_height": [0.05] * 4,
            "wheel_rads": [50.0] * 4, "g_force": [0.1, 0.2, 0.0],
            "gas_level": 40.0, "gas_capacity": 60.0,
        }
        if i == 0 and last_lap_ms is not None:
            fr["last_lap_ms"] = last_lap_ms
        out.append(fr)
    return out


def _write_session(path: Path, radius: float = 100.0,
                   laps: int = 3, n: int = 800) -> None:
    frames = []
    t = 1000.0
    for k in range(1, laps + 1):
        # 第 1 圈不给 last_lap_ms（起步圈没有"上一圈"）
        frames += _circle_lap(k, n, t, radius,
                              last_lap_ms=None if k == 1 else 24000 + k)
        t += n * 0.02
    lines = [json.dumps({"schema": 1, "layout": "A"})]
    lines += [json.dumps(f) for f in frames]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _register_track(hist: Path, names: list[str], track_id: int = 1) -> None:
    """把若干场次挂到一条赛道上（模拟系统识别过）。"""
    (hist / "tracks.json").write_text(json.dumps({
        "next_id": track_id + 1,
        "tracks": [{"id": track_id, "name": "Test Circle",
                    "ref_len_m": round(2 * math.pi * 100, 1),
                    "turns_cw": True, "created": "2026-10-22"}],
        "sessions": {n: {"track_id": track_id, "dist": 0.006} for n in names},
    }), encoding="utf-8")


@pytest.fixture()
def site(dash, tmp_path):
    (tmp_path / "_trash").mkdir(exist_ok=True)
    (tmp_path / "settings.json").write_text(
        json.dumps({"archive_after_days": 0}), encoding="utf-8")
    names = []
    for i, r in enumerate((100.0, 101.0, 99.5)):
        n = f"2026102{i + 1}_120000_x.jsonl"
        _write_session(tmp_path / n, radius=r)
        names.append(n)
    _register_track(tmp_path, names)

    (tmp_path / "status.json").write_text(json.dumps({
        "schema": 2, "seq": 1, "t": 1792599000.0,
        "last_rx_t": 1792599000.0, "frame": {"t": 1792599000.0},
        "history": [], "session_start": 1792598990.0, "frames_total": 0,
        "lap_times": [], "lap_fuel": [], "recording": True,
        "powertrain": "fuel", "max_energy_recovery": 0.0,
        "session_max_speed": 0.0,
    }), encoding="utf-8")
    dash.HUB = dash.TelemetryHub(tmp_path / "status.json")

    class Handler(dash.DashboardHandler):
        pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.history_dir = str(tmp_path)   # type: ignore[attr-defined]
    srv.allow_hosts = ()              # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield {"base": f"http://127.0.0.1:{srv.server_address[1]}",
               "hist": tmp_path, "dash": dash, "names": names}
    finally:
        srv.shutdown()
        srv.server_close()


def _call(base: str, method: str, path: str, body: dict | None = None):
    from urllib.parse import urlparse
    u = urlparse(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=120)
    try:
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read().decode("utf-8")
        try:
            return r.status, json.loads(raw)
        except json.JSONDecodeError:
            return r.status, {"_raw": raw}
    finally:
        c.close()


class TestGenerate:
    def test_生成成功并返回统计(self, site):
        st, d = _call(site["base"], "POST", "/api/v1/tracks/1/ref-line")
        assert st == 200, d
        assert d["track_id"] == 1
        assert d["n_laps"] == 3, f"三场应各贡献一条走线，实际 {d['n_laps']}"
        assert d["n_used"] >= 1
        assert len(d["pts"]) == d["n_points"]
        assert len(d["dist_m"]) == d["n_points"]
        assert d["len_m"] > 0
        assert d["source_note"] and "非 GT7 官方中心线" in d["source_note"]

    def test_离散度反映三场半径差异(self, site):
        """半径 99.5 / 100 / 101 → 走线差 0.5~1m，spread 应在 1m 量级。"""
        _, d = _call(site["base"], "POST", "/api/v1/tracks/1/ref-line")
        assert 0.2 < d["spread_m"] < 3.0, d["spread_m"]

    def test_每场的贡献都有记录(self, site):
        _, d = _call(site["base"], "POST", "/api/v1/tracks/1/ref-line")
        assert len(d["sources"]) == 3
        for s in d["sources"]:
            assert s["lap"] >= 1
            assert s["lap_s"] > 0
            assert s["len_m"] > 0

    def test_落盘到_ref_lines_json(self, site):
        _call(site["base"], "POST", "/api/v1/tracks/1/ref-line")
        fp = site["hist"] / "ref_lines.json"
        assert fp.is_file(), "生成后没有落盘 —— 重启就丢"
        data = json.loads(fp.read_text(encoding="utf-8"))
        assert "1" in data["tracks"]
        assert len(data["tracks"]["1"]["pts"]) == data["tracks"]["1"]["n_points"]

    def test_赛道不存在返回_400(self, site):
        st, d = _call(site["base"], "POST", "/api/v1/tracks/999/ref-line")
        assert st == 400
        assert "不存在" in d["error"]

    def test_id_不合法返回_400(self, site):
        st, d = _call(site["base"], "POST", "/api/v1/tracks/abc/ref-line")
        assert st == 400
        assert "不合法" in d["error"]

    def test_没有挂靠场次时给可执行的原因(self, site, dash):
        lib = json.loads((site["hist"] / "tracks.json").read_text(encoding="utf-8"))
        lib["tracks"].append({"id": 2, "name": "Empty"})
        (site["hist"] / "tracks.json").write_text(
            json.dumps(lib), encoding="utf-8")
        st, d = _call(site["base"], "POST", "/api/v1/tracks/2/ref-line")
        assert st == 400
        assert "还没有挂靠的场次" in d["error"]

    def test_max_sessions_被夹紧(self, site):
        _, d = _call(site["base"], "POST", "/api/v1/tracks/1/ref-line",
                     {"max_sessions": 1})
        assert d["n_laps"] == 1, "max_sessions=1 应该只用一场"


class TestRead:
    def test_未生成时_GET_返回_404_且不现场计算(self, site):
        """GET 必须只读缓存 —— 现场生成会让浏览器等几十秒。"""
        import time as _t
        t0 = _t.time()
        st, d = _call(site["base"], "GET", "/api/v1/tracks/1/ref-line")
        cost = _t.time() - t0
        assert st == 404, d
        assert "还没有生成" in d["error"]
        assert "POST" in d["hint"]
        assert cost < 5.0, f"GET 花了 {cost:.1f}s —— 它在现场生成？"

    def test_生成后能读回(self, site):
        _, made = _call(site["base"], "POST", "/api/v1/tracks/1/ref-line")
        st, got = _call(site["base"], "GET", "/api/v1/tracks/1/ref-line")
        assert st == 200
        assert got["n_laps"] == made["n_laps"]
        assert got["pts"] == made["pts"]
        assert got["meta"]["api_version"] == 1

    def test_重启后仍能读到(self, site):
        """清掉内存缓存，模拟容器重启 —— 必须从 ref_lines.json 读回来。"""
        _call(site["base"], "POST", "/api/v1/tracks/1/ref-line")
        dash = site["dash"]
        with dash._REF_LINES_LOCK:
            dash._REF_LINES_MEM.clear()
        st, got = _call(site["base"], "GET", "/api/v1/tracks/1/ref-line")
        assert st == 200, got
        assert got["n_laps"] == 3


class TestDoesNotPolluteTracksJson:
    def test_参考线不写进_tracks_json(self, site):
        """tracks.json 是元数据（改名/收藏频繁读写），5KB 的线不能塞进去。"""
        _call(site["base"], "POST", "/api/v1/tracks/1/ref-line")
        raw = (site["hist"] / "tracks.json").read_text(encoding="utf-8")
        assert "ref_line" not in raw, "参考线被塞进 tracks.json 了"
        assert len(raw) < 4000, f"tracks.json 被撑到 {len(raw)} 字节"


class TestTracksPageUi:
    """`/tracks` 页的参考走线区块。

    🔴 文案必须叫「参考走线」而不是「中心线」：前者是"你自己的历史走法"，
       后者听起来像 GT7 给的官方理想线。混淆会让人以为"应该照这条开"。
    """

    def _page(self, dash, hist: Path, ref: dict | None) -> str:
        (hist / "tracks.json").write_text(json.dumps({
            "next_id": 2,
            "tracks": [{"id": 1, "name": "Test Circle", "ref_len_m": 628.0,
                        "turns_cw": True, "created": "2026-10-22"}],
            "sessions": {},
        }), encoding="utf-8")
        if ref is not None:
            (hist / "ref_lines.json").write_text(json.dumps({
                "schema": 1, "tracks": {"1": ref}}), encoding="utf-8")
            with dash._REF_LINES_LOCK:
                dash._REF_LINES_MEM.clear()
        return dash.build_tracks_page(hist)

    def test_未生成时显示生成按钮(self, dash, tmp_path):
        html = self._page(dash, tmp_path, None)
        assert "参考走线：未生成" in html
        assert 'onclick="refBuild(this)"' in html
        assert "tk-refcv" in html, "缺少画布容器"

    def test_已生成时显示统计与查看按钮(self, dash, tmp_path):
        ref = {"pts": [[0, 0], [1, 1]], "dist_m": [0, 1], "n_points": 2,
               "n_laps": 5, "n_used": 4, "len_m": 5019.5, "spread_m": 1.07,
               "built": "2026-10-22 09:30",
               "source_note": "本车历史最快圈的中位数走线（非 GT7 官方中心线）"}
        html = self._page(dash, tmp_path, ref)
        assert "参考走线：4/5 圈" in html
        assert "5020" in html, "圈长没显示（5019.5 四舍五入成 5020）"
        assert "1.1 m" in html, "圈间差没显示"
        assert 'onclick="refShow(this)"' in html
        assert 'onclick="refBuild(this)"' in html, "已生成也应能重建"

    def test_用参考走线而不是中心线(self, dash, tmp_path):
        html = self._page(dash, tmp_path, None)
        assert "参考走线" in html
        assert "中心线" not in html, (
            "页面上出现了「中心线」—— 会让人以为这是 GT7 官方理想线")

    def test_列表接口只给摘要不给点(self, dash, tmp_path):
        """每条约 5KB，塞进列表会让 /api/v1/tracks 变重。"""
        ref = {"pts": [[0, 0], [1, 1]], "dist_m": [0, 1], "n_points": 2,
               "n_laps": 3, "n_used": 3, "len_m": 628.0, "spread_m": 0.5,
               "built": "2026-10-22 09:30",
               "source_note": "本车历史最快圈的中位数走线（非 GT7 官方中心线）"}
        summary = dash._ref_line_summary({"1": ref}, 1)
        assert summary["n_laps"] == 3
        assert "pts" not in summary, "摘要里不该带点列"
        assert dash._ref_line_summary({}, 1) is None


class TestSessionDedup:
    """同一场次的 `.jsonl` 与 `.jsonl.gz` 两个键不能算两场。

    🔴 真实数据踩到：服务器上赛道 41 有 8 个 sessions 键，实际只有 6 个场次
       （归档前后各注册过一次）。后果是「N 场」翻倍，参考走线把同一场算两遍
       —— 中位数被它拉偏，而且看不出来（统计里只有数字）。
    """

    def _lib(self, keys: dict) -> dict:
        return {"next_id": 2,
                "tracks": [{"id": 1, "name": "T"}],
                "sessions": {k: {"track_id": 1, "dist": 0.01} for k in keys}}

    def test_同一场次两个键只算一场(self, dash):
        lib = self._lib({"20260101_120000_a.jsonl": 1,
                         "20260101_120000_a.jsonl.gz": 1})
        assert dash._track_session_counts(lib)[1] == 1
        assert dash._unique_track_sessions(lib, 1) == ["20260101_120000_a.jsonl"]

    def test_优先保留未压缩名(self, dash):
        # 顺序反过来也要选 .jsonl
        lib = self._lib({"20260101_120000_a.jsonl.gz": 1,
                         "20260101_120000_a.jsonl": 1})
        assert dash._unique_track_sessions(lib, 1) == ["20260101_120000_a.jsonl"]

    def test_只有压缩版时用它(self, dash):
        lib = self._lib({"20260101_120000_a.jsonl.gz": 1})
        assert dash._unique_track_sessions(lib, 1) == ["20260101_120000_a.jsonl.gz"]

    def test_不同场次都保留且新的在前(self, dash):
        lib = self._lib({"20260101_120000_a.jsonl": 1,
                         "20260102_120000_b.jsonl": 1,
                         "20260102_120000_b.jsonl.gz": 1,
                         "20260103_120000_c.jsonl": 1})
        assert dash._track_session_counts(lib)[1] == 3
        assert dash._unique_track_sessions(lib, 1) == [
            "20260103_120000_c.jsonl",
            "20260102_120000_b.jsonl",
            "20260101_120000_a.jsonl"]

    def test_别的赛道不受影响(self, dash):
        lib = self._lib({"20260101_120000_a.jsonl": 1})
        lib["sessions"]["20260101_130000_x.jsonl"] = {"track_id": 2}
        assert dash._track_session_counts(lib) == {1: 1, 2: 1}
        assert dash._unique_track_sessions(lib, 1) == ["20260101_120000_a.jsonl"]

    def test_生成参考走线不重复算同一场(self, site):
        """tracks.json 里塞一份 .gz 别名，n_laps 不该变成 6。"""
        hist = site["hist"]
        lib = json.loads((hist / "tracks.json").read_text(encoding="utf-8"))
        # 给第一场加一个 .gz 别名（模拟归档前后各注册一次）
        alias = site["names"][0].replace(".jsonl", ".jsonl.gz")
        lib["sessions"][alias] = {"track_id": 1, "dist": 0.006}
        (hist / "tracks.json").write_text(json.dumps(lib), encoding="utf-8")
        _, d = _call(site["base"], "POST", "/api/v1/tracks/1/ref-line")
        assert d["n_laps"] == 3, f"别名被算成独立场次了：{d['n_laps']}"
        names = [s["session"] for s in d["sources"]]
        assert len(set(names)) == 3, f"同一场出现两次：{names}"


class TestSessionDedup:
    """同一场次的 `.jsonl` 与 `.jsonl.gz` 两个键不能算两场。

    🔴 真实数据踩到：服务器上赛道 41 有 8 个 sessions 键，实际只有 6 个场次
       （归档前后各注册过一次）。后果是「N 场」翻倍，参考走线把同一场算两遍
       —— 中位数被它拉偏，而且看不出来（统计里只有数字）。
    """

    def _lib(self, keys: dict) -> dict:
        return {"next_id": 2,
                "tracks": [{"id": 1, "name": "T"}],
                "sessions": {k: {"track_id": 1, "dist": 0.01} for k in keys}}

    def test_同一场次两个键只算一场(self, dash):
        lib = self._lib({"20260101_120000_a.jsonl": 1,
                         "20260101_120000_a.jsonl.gz": 1})
        assert dash._track_session_counts(lib)[1] == 1
        assert dash._unique_track_sessions(lib, 1) == ["20260101_120000_a.jsonl"]

    def test_优先保留未压缩名(self, dash):
        # 顺序反过来也要选 .jsonl
        lib = self._lib({"20260101_120000_a.jsonl.gz": 1,
                         "20260101_120000_a.jsonl": 1})
        assert dash._unique_track_sessions(lib, 1) == ["20260101_120000_a.jsonl"]

    def test_只有压缩版时用它(self, dash):
        lib = self._lib({"20260101_120000_a.jsonl.gz": 1})
        assert dash._unique_track_sessions(lib, 1) == ["20260101_120000_a.jsonl.gz"]

    def test_不同场次都保留且新的在前(self, dash):
        lib = self._lib({"20260101_120000_a.jsonl": 1,
                         "20260102_120000_b.jsonl": 1,
                         "20260102_120000_b.jsonl.gz": 1,
                         "20260103_120000_c.jsonl": 1})
        assert dash._track_session_counts(lib)[1] == 3
        assert dash._unique_track_sessions(lib, 1) == [
            "20260103_120000_c.jsonl",
            "20260102_120000_b.jsonl",
            "20260101_120000_a.jsonl"]

    def test_别的赛道不受影响(self, dash):
        lib = self._lib({"20260101_120000_a.jsonl": 1})
        lib["sessions"]["20260101_130000_x.jsonl"] = {"track_id": 2}
        assert dash._track_session_counts(lib) == {1: 1, 2: 1}
        assert dash._unique_track_sessions(lib, 1) == ["20260101_120000_a.jsonl"]

    def test_生成参考走线不重复算同一场(self, site):
        """tracks.json 里塞一份 .gz 别名，n_laps 不该变成 6。"""
        hist = site["hist"]
        lib = json.loads((hist / "tracks.json").read_text(encoding="utf-8"))
        # 给第一场加一个 .gz 别名（模拟归档前后各注册一次）
        alias = site["names"][0].replace(".jsonl", ".jsonl.gz")
        lib["sessions"][alias] = {"track_id": 1, "dist": 0.006}
        (hist / "tracks.json").write_text(json.dumps(lib), encoding="utf-8")
        _, d = _call(site["base"], "POST", "/api/v1/tracks/1/ref-line")
        assert d["n_laps"] == 3, f"别名被算成独立场次了：{d['n_laps']}"
        names = [s["session"] for s in d["sources"]]
        assert len(set(names)) == 3, f"同一场出现两次：{names}"
