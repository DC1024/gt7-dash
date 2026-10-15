"""历史列表「地图」列的缩略轨迹取数（session_thumb）+ 路由顺序守卫。

- session_thumb：取代表圈的降采样 [x, z] 折线，给历史页每行画小地图用。
- /api/v1/sessions/thumbs：一次性取所有场的缩略轨迹，必须排在通用
  `/api/v1/sessions/<名>` 路由之前，否则 /thumbs 会被当成场次名吞掉。
"""
import json

import pytest


def _write_session(path, laps=3, frames_per_lap=300, hz=60.0, base_t=1000.0,
                    coords=True):
    """合成多圈场次；coords=False 时整场都不下发 car_x/car_z（测无轨迹分支）。"""
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
                # 让每圈是个闭合环（z 随圈推进），缩略图才像赛道
                row["car_x"] = 100.0 + 90.0 * (i / max(1, frames_per_lap - 1))
                row["car_z"] = 50.0 + 40.0 * (lap + ph)
            lines.append(json.dumps(row, ensure_ascii=False))
            t += 1 / hz
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def sess(dash, tmp_path):
    return _write_session(tmp_path / "20261008_000000_unknown_abcdef01.jsonl")


class TestSessionThumb:
    def test_返回降采样折线(self, dash, sess):
        p = dash.session_thumb(sess, max_points=120)
        assert len(p) >= 2, "有效场次必须有轨迹点"
        assert len(p) <= 120, "不应超过 max_points（首尾补点也不应爆）"
        for x, z in p:
            assert isinstance(x, (int, float)) and isinstance(z, (int, float))
            assert x is not None and z is not None

    def test_只取一圈作代表(self, dash, sess):
        """代表圈 = 点最多的有效圈；3 圈同帧数 → 取其中一圈的全部点（不跨圈拼）。"""
        p = dash.session_thumb(sess, max_points=9999)
        # 单圈 frames_per_lap=300，降采样上限拉到很大也不会超过单圈点数
        assert len(p) <= 300, "缩略图不应跨多圈拼接"

    def test_无坐标返回空(self, dash, tmp_path):
        s = _write_session(tmp_path / "nocoord.jsonl", coords=False)
        assert dash.session_thumb(s) == [], "没有坐标的场次应返回 []（前端显示占位）"

    def test_max_points_钳制(self, dash, sess):
        # 单圈 300 点，max_points=8 → 降到 ~9（含补的末点）
        p = dash.session_thumb(sess, max_points=8)
        assert 2 <= len(p) <= 10


def test_thumbs_路由排在通用场次路由之前(dash):
    """🔴 /thumbs 必须排在 `path.startswith('/api/v1/sessions/')` 的通用分支之前，
    否则会被当成场次名 → 404，历史页缩略图全失败。"""
    src = open(dash.__file__, encoding="utf-8").read()
    i_thumbs = src.find('path == "/api/v1/sessions/thumbs"')
    i_generic = src.find('elif path.startswith("/api/v1/sessions/"):')
    assert i_thumbs != -1, "缺少 /thumbs 分支"
    assert i_generic != -1, "缺少通用场次路由（不该发生）"
    assert i_thumbs < i_generic, "路由顺序错了：/thumbs 必须排在通用场次路由之前"
