"""历史列表「赛道」列（赛道名文字）+ 后台识别补充机制回归。

- 2026-10-15 起，历史列表「地图」列改为赛道名文字：识别复用详情页的
  session_track，结果持久化到 data/tracks.json，列表页零解析开销。
- 缺识别的场次由 _kick_track_identify_sweep 在后台线程补，不阻塞列表响应。
- 缩略图链路（session_thumb / /thumbs）已移除，改赛道名文字。
"""
import json

import pytest


def _write_session(path, laps=3, frames_per_lap=1500, hz=60.0, base_t=1000.0,
                    coords=True):
    """合成多圈场次；coords=False 时整场都不下发 car_x/car_z。"""
    lines = [json.dumps({"session_id": path.stem, "circuit": "test",
                         "car": 1302, "powertrain": "fuel"}, ensure_ascii=False)]
    t = base_t
    for lap in range(1, laps + 1):
        for i in range(frames_per_lap):
            ph = i / frames_per_lap
            row = {
                "t": round(t, 4), "lap": lap,
                "speed_kph": round(180.0 + 20 * ph, 2),
                "throttle": round(max(0.0, 1 - 2 * ph), 3),
                "brake": round(max(0.0, 2 * ph - 1), 3),
                "g_force": [0.3, 0.1, 0.0],
            }
            if coords:
                row["car_x"] = 100.0 + 90.0 * (i / max(1, frames_per_lap - 1))
                row["car_z"] = 50.0 + 40.0 * (lap + ph)
            lines.append(json.dumps(row, ensure_ascii=False))
            t += 1 / hz
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def sess(tmp_path):
    return _write_session(tmp_path / "20261008_000000_unknown_abcdef01.jsonl")


class TestListSessionsTrack:
    def test_列表每行含_track_name_字段(self, dash, sess):
        """模板读 s['track_name']，缺字段会 KeyError；必须保证字段存在。"""
        rows = dash.list_sessions(sess.parent)
        assert isinstance(rows, list) and rows, "list_sessions 应返回非空"
        for r in rows:
            assert "track_name" in r, "缺少 track_name 字段（模板会崩）"
            assert "track_id" in r, "缺少 track_id 字段"

    def test_无坐标场次也不崩(self, dash, tmp_path):
        s = _write_session(tmp_path / "nocoord.jsonl", coords=False)
        rows = dash.list_sessions(s.parent)
        assert any(r["file"] == s.name for r in rows)
        for r in rows:
            assert "track_name" in r


def test_session_track_调用不崩(dash, sess):
    """识别链路（指纹 + 落库）在移除缩略图后仍然工作，且不抛异常。"""
    res = dash.session_track(sess)
    assert isinstance(res, dict), "session_track 应返回 dict"


def test_后台补不并发重复启动(dash, tmp_path):
    """_kick_track_identify_sweep：pending 空直接返回；RUNNING 中不重复启动。"""
    dash._TRACK_IDENT_RUNNING = False
    dash._kick_track_identify_sweep(tmp_path, [])
    assert dash._TRACK_IDENT_RUNNING is False

    s = _write_session(tmp_path / "x.jsonl")
    dash._TRACK_IDENT_RUNNING = True
    try:
        dash._kick_track_identify_sweep(tmp_path, [s])
        assert dash._TRACK_IDENT_RUNNING is True
    finally:
        dash._TRACK_IDENT_RUNNING = False


def test_路由已移除_thumbs(dash):
    """缩略图端点已移除；源码不应再出现 /api/v1/sessions/thumbs 分支。"""
    src = open(dash.__file__, encoding="utf-8").read()
    assert 'path == "/api/v1/sessions/thumbs"' not in src, \
        "缩略图 /thumbs 路由应已移除"
