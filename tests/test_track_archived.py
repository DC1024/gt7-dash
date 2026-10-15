# -*- coding: utf-8 -*-
"""「半圈赛道」自愈 + 0 场赛道自动归档（可恢复/可彻底删除）。

2026-10-16 实测：SUGO 第一圈只跑到 689m 时被后台识别扫到，半截文件的
指纹建出一条 689m 的假赛道，且会话映射落库后永远走快路径、再不重算。
修复三件套：
  1. 映射带文件 sig（mtime+size），对不上就作废重算（自愈并回真赛道）；
  2. 识别队列只收「已跑完」场次（异常/活场不进队）；
  3. 重算后一场不挂的空壳赛道自动进「已归档」，UI 可恢复/彻底删除。
"""
import json
import math
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


# ---------- 合成数据 ----------

def _write_session(path: Path, frames) -> None:
    path.write_text(
        json.dumps({"header": True}) + "\n"
        + "".join(json.dumps(fr) + "\n" for fr in frames),
        encoding="utf-8",
    )


def _circle_frames(lap=1, samples=1500, radius=100.0, t0=0.0, full=True):
    """合成整圆圈。full=False 时只生成前 1/4 圈（模拟录制到一半的活场）。"""
    n = samples if full else samples // 4
    return [{"t": t0 + i * 0.033, "lap": lap,
             "car_x": math.cos(2 * math.pi * i / samples) * radius,
             "car_z": math.sin(2 * math.pi * i / samples) * radius}
            for i in range(n)]


# ---------- HTTP fixture（与 test_track_ui_and_trash_view.py 同一套） ----------

@pytest.fixture(scope="module")
def srv(dash, tmp_path_factory):
    hist = tmp_path_factory.mktemp("data-track-arch")
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
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _grow_file(path: Path, frames) -> None:
    """模拟「车继续跑」：重写文件让 mtime/size 变化。"""
    time.sleep(0.02)
    _write_session(path, frames)


# ---------- 自愈 ----------

class TestHalfLapSelfHeal:
    def test_半圈假赛道_跑完后并回真赛道且空壳归档(self, dash, tmp_path):
        d = tmp_path
        f1 = d / "20260101_120000_unknown.jsonl"
        f2 = d / "20260101_130000_unknown.jsonl"
        _write_session(f1, _circle_frames())
        _write_session(f2, _circle_frames(t0=500.0, full=False))

        r1 = dash.session_track(f1)
        real_tid = r1["track_id"]
        r2 = dash.session_track(f2)
        fake_tid = r2["track_id"]
        assert fake_tid != real_tid, "1/4 圈指纹不应命中整圆赛道"

        lib = dash.load_tracks(d)
        fake = next(t for t in lib["tracks"] if t["id"] == fake_tid)
        real = next(t for t in lib["tracks"] if t["id"] == real_tid)
        assert fake["ref_len_m"] < real["ref_len_m"] * 0.5, \
            "假赛道圈长应约为真赛道的 1/4"

        _grow_file(f2, _circle_frames(t0=500.0, full=True))
        r2b = dash.session_track(f2)
        assert r2b["track_id"] == real_tid, "跑完后的指纹应命中真赛道"

        lib = dash.load_tracks(d)
        fake = next(t for t in lib["tracks"] if t["id"] == fake_tid)
        real = next(t for t in lib["tracks"] if t["id"] == real_tid)
        assert fake.get("archived") is True, "0 场次假赛道应被自动归档"
        assert fake.get("archived_reason")
        assert lib["sessions"][f2.name]["track_id"] == real_tid
        assert lib["sessions"][f1.name]["track_id"] == real_tid

    def test_快路径对老映射_无sig_重算一次并落sig(self, dash, tmp_path):
        """老库映射没存 sig → 视为待重算；重算后写入 sig，下次走快路径。"""
        d = tmp_path
        f = d / "20260101_120000_unknown.jsonl"
        _write_session(f, _circle_frames())
        r1 = dash.session_track(f)
        tid = r1["track_id"]

        # 手工把 sig 抹掉，模拟「sig 机制上线前的老映射」
        lib = dash.load_tracks(d)
        del lib["sessions"][f.name]["sig"]
        dash.save_tracks(d, lib)

        r2 = dash.session_track(f)
        assert r2["track_id"] == tid, "内容没变，重算应命中同一条"
        lib = dash.load_tracks(d)
        assert lib["sessions"][f.name].get("sig"), "重算后应写入 sig"

        # 有 sig 且文件没变 → 走快路径（映射不被动）
        import copy
        before = copy.deepcopy(lib["sessions"][f.name])
        r3 = dash.session_track(f)
        assert r3["track_id"] == tid
        lib = dash.load_tracks(d)
        assert lib["sessions"][f.name] == before

    def test_识别队列_异常场与活场不进队(self, dash, tmp_path):
        # —— 场景 1：录制中（status.json 新鲜）——
        # live = 录制中且文件 20s 内被写过，此时全场都是「文件还在长」的活场，
        # 一个都不进识别队列（拿去算指纹就是半截的）。
        d = tmp_path
        f_anom = d / "20260101_120000_unknown.jsonl"
        _write_session(f_anom,
                       [{"lap": 0, "t": float(i) / 10.0} for i in range(5)])
        f_live = d / "20260101_130000_unknown.jsonl"
        _write_session(f_live, _circle_frames(full=False))
        (d / "status.json").write_text(
            json.dumps({"recording": True, "t": time.time()}),
            encoding="utf-8")

        sess = dash.list_sessions(d)
        rows = {s["file"]: s for s in sess}
        assert rows[f_anom.name]["anomalous"] is True
        assert rows[f_anom.name]["live"] is True, "录制中的异常场也受活场保护"
        assert rows[f_live.name]["live"] is True

        # —— 场景 2：没在录制 ——异常场不进队，正常场进队 ——
        d2 = d / "done"
        d2.mkdir()
        f_done = d2 / "20260101_140000_unknown.jsonl"
        _write_session(f_done, _circle_frames())
        dash.list_sessions(d2)
        deadline = time.time() + 15
        while time.time() < deadline:
            lib = dash.load_tracks(d2)
            if f_done.name in lib["sessions"]:
                break
            time.sleep(0.2)
        lib = dash.load_tracks(d2)
        assert f_done.name in lib["sessions"], "跑完的场次应被补识别"

        # 场景 1 的目录：等后台线程安静后确认异常场/活场都没被识别
        deadline = time.time() + 3
        time.sleep(1.0)   # 给可能的误识别线程留足时间（若判据失效它会落库）
        lib = dash.load_tracks(d)
        assert f_anom.name not in lib["sessions"], "异常场次不应被识别"
        assert f_live.name not in lib["sessions"], "活场不应被识别"


# ---------- 归档区 ----------

class TestArchivedTrackEndpoints:
    def test_restore_清归档标记(self, srv):
        hist = Path(srv.history_dir)
        (hist / "tracks.json").write_text(json.dumps({
            "next_id": 2,
            "tracks": [{"id": 1, "name": "半圈壳", "desc": [[0, 0], [1, 1]],
                        "ref_len_m": 688.7, "archived": True,
                        "archived_at": "2026-10-16 10:00",
                        "archived_reason": "识别结果已并回其它赛道（无场次）"}],
            "sessions": {},
        }), encoding="utf-8")
        st, body = _req(srv.server_address[1], "POST",
                        "/api/v1/tracks/1/restore", {})
        assert st == 200 and json.loads(body)["ok"] is True
        saved = json.loads((hist / "tracks.json").read_text(encoding="utf-8"))
        t = saved["tracks"][0]
        assert "archived" not in t and "archived_at" not in t

    def test_delete_空壳可删_挂靠中409(self, srv, dash):
        hist = Path(srv.history_dir)
        (hist / "tracks.json").write_text(json.dumps({
            "next_id": 3,
            "tracks": [
                {"id": 1, "name": "空壳", "desc": [[0, 0]], "ref_len_m": 100},
                {"id": 2, "name": "在用", "desc": [[0, 0]], "ref_len_m": 200}],
            "sessions": {"x.jsonl": {"track_id": 2}},
        }), encoding="utf-8")
        base = srv.server_address[1]
        st, _ = _req(base, "POST", "/api/v1/tracks/2/delete", {})
        assert st == 409, "挂靠中的赛道不能删"
        st, body = _req(base, "POST", "/api/v1/tracks/1/delete", {})
        assert st == 200 and json.loads(body)["deleted"] is True
        saved = json.loads((hist / "tracks.json").read_text(encoding="utf-8"))
        assert [t["id"] for t in saved["tracks"]] == [2]

    def test_tracks接口带archived字段(self, srv):
        hist = Path(srv.history_dir)
        (hist / "tracks.json").write_text(json.dumps({
            "next_id": 2,
            "tracks": [
                {"id": 1, "name": "正常", "desc": [], "ref_len_m": 100},
                {"id": 2, "name": "已归档", "desc": [], "ref_len_m": 200,
                 "archived": True, "archived_at": "2026-10-16 10:00"}],
            "sessions": {},
        }), encoding="utf-8")
        st, body = _req(srv.server_address[1], "GET", "/api/v1/tracks")
        assert st == 200
        items = {t["id"]: t for t in json.loads(body)["tracks"]}
        assert items[1]["archived"] is False
        assert items[2]["archived"] is True
        assert items[2]["archived_at"] == "2026-10-16 10:00"


class TestArchivedTrackUI:
    def test_归档赛道进折叠区_带恢复删除按钮(self, dash):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td)
            (hist / "tracks.json").write_text(json.dumps({
                "next_id": 3,
                "tracks": [
                    {"id": 1, "name": "SUGO国际赛车场", "desc": [[0, 0], [1, 1]],
                     "ref_len_m": 3541, "turns_cw": True},
                    {"id": 2, "name": "半圈壳", "desc": [[0, 0]],
                     "ref_len_m": 689, "archived": True,
                     "archived_at": "2026-10-16 10:00",
                     "archived_reason": "识别结果已并回其它赛道（无场次）"}],
                "sessions": {"a.jsonl": {"track_id": 1}},
            }), encoding="utf-8")
            html = dash.build_tracks_page(hist)
            # 主网格只有未归档的；归档的在 <details class="tk-arch"> 里
            assert 'class="tk-arch"' in html
            assert "半圈壳" in html
            assert "trackRestore(this)" in html
            assert "trackDelete(this)" in html
            assert "归档于 2026-10-16 10:00" in html
            # 主卡片部分不再出现归档赛道（归档卡只出现在 tk-arch 折叠区之后）
            grid_end = html.index("</details>")
            grid = html[:html.index('<details class="tk-arch">')]
            assert "半圈壳" not in grid

    def test_无归档赛道时不渲染折叠区(self, dash):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td)
            (hist / "tracks.json").write_text(json.dumps({
                "next_id": 2,
                "tracks": [{"id": 1, "name": "只有一条", "desc": [[0, 0]],
                            "ref_len_m": 1000}],
                "sessions": {},
            }), encoding="utf-8")
            html = dash.build_tracks_page(hist)
            assert 'class="tk-arch"' not in html


# ---------- gc ----------

class TestGcEmptyTracks:
    def test_挂靠中不归档_空壳归档_已归档不重复打标(self, dash):
        lib = {"tracks": [
            {"id": 1, "name": "在用"},
            {"id": 2, "name": "空壳"},
            {"id": 3, "name": "已归档", "archived": True},
            {"id": 4, "name": "用户确认过", "verified": True},
        ], "sessions": {"a.jsonl": {"track_id": 1}}}
        assert dash._gc_empty_tracks(lib) == 1
        assert not lib["tracks"][0].get("archived")
        assert lib["tracks"][1]["archived"] is True
        assert lib["tracks"][2].get("archived") is True   # 原样保留
        assert not lib["tracks"][3].get("archived"), "verified 不动"
        # 再跑：没有新增
        assert dash._gc_empty_tracks(lib) == 0
