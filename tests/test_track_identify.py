# -*- coding: utf-8 -*-
"""赛道自动识别（dashboard 层）—— session_track 识别 / 快路径 / 改名。

用合成圆轨迹造场次：两场同赛道（同形状）应命中同一条赛道记录，
第三场不同形状（椭圆）应新建一条。改名后两场的名字一起变。
"""
import json
import math

from pathlib import Path


def _write_session(path: Path, frames) -> None:
    path.write_text(
        json.dumps({"header": True}) + "\n"
        + "".join(json.dumps(fr) + "\n" for fr in frames),
        encoding="utf-8",
    )


def _circle_frames(lap=1, samples=1500, radius=100.0, t0=0.0,
                   ccw=True, cx=0.0, cz=0.0):
    sgn = 1.0 if ccw else -1.0
    return [{"t": t0 + i * 0.033, "lap": lap,
             "car_x": cx + math.cos(2 * math.pi * i / samples) * radius,
             "car_z": cz + math.sin(2 * math.pi * i / samples) * radius * sgn}
            for i in range(samples)]


def _oval_frames(lap=1, samples=1500, t0=0.0):
    return [{"t": t0 + i * 0.033, "lap": lap,
             "car_x": math.cos(2 * math.pi * i / samples) * 200.0,
             "car_z": math.sin(2 * math.pi * i / samples) * 80.0}
            for i in range(samples)]


def test_identify_match_and_new_track(dash, tmp_path):
    d = tmp_path
    f1 = d / "20260101_120000_unknown.jsonl"
    f2 = d / "20260101_130000_unknown.jsonl"
    f3 = d / "20260101_140000_unknown.jsonl"
    _write_session(f1, _circle_frames())
    _write_session(f2, _circle_frames(t0=500.0))          # 同形状（另一场）
    _write_session(f3, _oval_frames())                    # 不同形状

    r1 = dash.session_track(f1)
    assert r1["track_id"] is not None and not r1["matched"]
    assert r1["name"] == "", "首次识别无库可匹配，应为未命名新赛道"

    r2 = dash.session_track(f2)
    assert r2["matched"] is True, "同形状第二场应命中第一条赛道"
    assert r2["track_id"] == r1["track_id"]
    assert r2["distance"] < dash._TRACK_MATCH_TOL

    r3 = dash.session_track(f3)
    assert r3["matched"] is False, "椭圆与圆距离应超阈值"
    assert r3["track_id"] != r1["track_id"], "不同形状应新建赛道"

    lib = dash.load_tracks(d)
    assert lib["next_id"] == 3
    assert len(lib["tracks"]) == 2
    assert set(lib["sessions"]) == {f1.name, f2.name, f3.name}


def test_fast_path_and_rename(dash, tmp_path):
    d = tmp_path
    f1 = d / "20260101_120000_unknown.jsonl"
    _write_session(f1, _circle_frames())

    r1 = dash.session_track(f1)
    tid = r1["track_id"]

    # 改名
    lib = dash.load_tracks(d)
    next(t for t in lib["tracks"] if t["id"] == tid)["name"] = "测试场"
    dash.save_tracks(d, lib)

    # 快路径：不再解析 jsonl 也应拿到新名字
    r2 = dash.session_track(f1)
    assert r2["name"] == "测试场"
    assert r2["matched"] is True

    # 列表页带出赛道名
    sess = dash.list_sessions(d)
    row = next(s for s in sess if s["file"] == f1.name)
    assert row["track_name"] == "测试场"
    assert row["track_id"] == tid


def test_session_without_valid_laps_is_error(dash, tmp_path):
    """没有有效圈的场次（如纯菜单态）→ error，不落库不抛错。"""
    d = tmp_path
    f = d / "20260101_120000_unknown.jsonl"
    _write_session(f, [{"lap": 0, "t": 0.0, "car_x": 1.0, "car_z": 1.0}])
    r = dash.session_track(f)
    assert "error" in r
    lib = dash.load_tracks(d)
    assert lib["tracks"] == [] and lib["sessions"] == {}