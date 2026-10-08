"""逐帧遥测接口（/series、/frames、/csv）的纯函数测试。

这三个函数是详情页「遥测数据」卡片的数据源，负责把 jsonl 里的逐帧数据
换成「能画曲线 / 能翻页 / 能导出」的形状。测的点：
  · 抽稀不能丢末端（否则曲线右端凭空少一截）
  · 分页边界与 total 要对得上（否则翻到最后一页是空的）
  · CSV 表头与列数要和表格一致（否则 Excel 打开错位）
"""
import json

import pytest


def _write_session(path, laps=3, frames_per_lap=1200, hz=60.0, base_t=1000.0):
    """合成一个多圈场次：每圈匀速、油门/刹车交替，距离相同避免被判假圈。"""
    lines = [json.dumps({"session_id": path.stem, "circuit": "test",
                         "car": 1302, "powertrain": "fuel"},
                        ensure_ascii=False)]
    t = base_t
    for lap in range(1, laps + 1):
        for i in range(frames_per_lap):
            ph = i / frames_per_lap
            lines.append(json.dumps({
                "t": round(t, 4), "lap": lap,
                "speed_kph": round(180.0 + 20 * ph, 2),
                "rpm": round(5000 + 1000 * ph, 1),
                "gear": 4,
                "throttle": round(max(0.0, 1 - 2 * ph), 3),
                "brake": round(max(0.0, 2 * ph - 1), 3),
                "g_force": [round(0.5 - ph, 3), round(ph - 0.5, 3), 0.0],
                "gas_level": 60.0, "gas_capacity": 100.0,
                "car_x": 100 + i, "car_z": 50.0,
                "tyre_temp": [85, 86, 85, 86],
                "susp_height": [0.03] * 4,
                "has_coords": True,
            }, ensure_ascii=False))
            t += 1 / hz
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def sess(dash, tmp_path):
    return _write_session(tmp_path / "20261008_000000_unknown_abcdef01.jsonl")


class TestSessionSeries:
    def test_shape_and_columns(self, dash, sess):
        d = dash.session_series(sess, max_points=500)
        assert d["cols"][:5] == ["t", "spd", "rpm", "thr", "brk"]
        assert d["rows"], "应当有样本行"
        assert all(len(r) == len(d["cols"]) for r in d["rows"])
        assert d["total_frames"] == 3 * 1200

    def test_downsample_respects_max_points(self, dash, sess):
        d = dash.session_series(sess, max_points=300)
        assert len(d["rows"]) <= 300 + 1, "抽稀后不应超过上限太多（+1 是补的末帧）"
        assert d["step"] > 1, "300 点画 3600 帧，必须抽稀"

    def test_last_frame_kept(self, dash, sess):
        """抽稀最容易踩的坑：丢掉末帧 → 曲线右端少一截。"""
        d = dash.session_series(sess, max_points=257)
        # 末帧的相对时间应接近全场时长（3600 帧 / 60Hz = 60s）
        assert d["rows"][-1][0] > 59.0, f"末帧被抽掉了？最后 t={d['rows'][-1][0]}"

    def test_lap_filter_and_scope_count(self, dash, sess):
        d = dash.session_series(sess, lap_no=2, max_points=1000)
        assert d["lap"] == 2
        assert d["scope_frames"] == 1200
        assert d["total_frames"] == 3600, "整场帧数不受选圈影响"
        # 单圈时 x 轴应从 0 附近起算（相对该圈起点）
        assert d["rows"][0][0] == pytest.approx(0.0, abs=0.05)

    def test_laps_listed_with_duration(self, dash, sess):
        d = dash.session_series(sess)
        assert [l["lap"] for l in d["laps"]] == [1, 2, 3]
        for l in d["laps"]:
            assert l["dur"] == pytest.approx(1200 / 60.0, abs=0.05)
            assert l["frames"] == 1200

    def test_bad_lap_returns_error(self, dash, sess):
        d = dash.session_series(sess, lap_no=99)
        assert "error" in d


class TestSessionFrames:
    def test_pagination(self, dash, sess):
        p1 = dash.session_frames(sess, offset=0, limit=200)
        assert p1["total"] == 3600
        assert p1["offset"] == 0 and p1["limit"] == 200
        assert len(p1["rows"]) == 200
        p2 = dash.session_frames(sess, offset=200, limit=200)
        assert len(p2["rows"]) == 200
        assert p2["rows"][0][0] != p1["rows"][0][0], "第二页应当是别的帧"

    def test_last_page_partial(self, dash, sess):
        p = dash.session_frames(sess, offset=3500, limit=200)
        assert len(p["rows"]) == 100
        assert p["total"] == 3600

    def test_limit_is_capped(self, dash, sess):
        """limit 不设上限的话，一个请求就能把浏览器拉爆。"""
        p = dash.session_frames(sess, offset=0, limit=999999)
        assert p["limit"] == 1000
        assert len(p["rows"]) == 1000

    def test_lap_filter(self, dash, sess):
        p = dash.session_frames(sess, offset=0, limit=50, lap_no=3)
        assert p["total"] == 1200
        assert all(r[9] == 3 for r in p["rows"]), "第 9 列（圈号）应全是 3"

    def test_row_values_are_readable(self, dash, sess):
        p = dash.session_frames(sess, offset=0, limit=1)
        t, spd, rpm, thr, brk, gear, glat, glon, fuel, lap = p["rows"][0]
        assert t == 0.0
        assert 180 <= spd <= 200
        assert 5000 <= rpm <= 6000
        assert thr == 100 and brk == 0, "第一帧是满油零刹"
        assert gear == 4 and lap == 1
        assert fuel == 60.0


class TestSessionCsv:
    def test_header_and_row_count(self, dash, sess):
        txt = dash.session_csv(sess)
        lines = txt.strip().split("\n")
        assert lines[0].count(",") == 9, "表头 10 列"
        assert len(lines) == 1 + 3600

    def test_lap_filter(self, dash, sess):
        txt = dash.session_csv(sess, lap_no=1)
        lines = txt.strip().split("\n")
        assert len(lines) == 1 + 1200
        assert all(l.split(",")[-1] == "1" for l in lines[1:])

    def test_no_none_leaks_into_csv(self, dash, sess):
        """电车油量没有百分比口径（cap=0）→ None，CSV 里必须是空而不是 'None'。"""
        assert "None" not in dash.session_csv(sess)


class TestFrameRow:
    def test_fuel_none_for_electric(self, dash):
        row = dash._frame_row({"t": 5.0, "gas_level": 30.0, "gas_capacity": 0.0}, 0.0)
        assert row[8] is None, "纯电车 cap=0，不该编造油量百分比"

    def test_throttle_brake_scaled_to_percent(self, dash):
        row = dash._frame_row({"t": 0, "throttle": 0.63, "brake": 0.5}, 0.0)
        assert row[3] == 63 and row[4] == 50
