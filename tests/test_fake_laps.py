"""假圈剔除 / 参考圈选择 / 车型解析 —— 回归测试。

这些用例全部按**真实场次 jsonl 的实测特征**构造：
  - 前圈：比赛开始前车静止（lap=0，96% 帧速度 0）
  - 末圈：完赛后松油滑行离场（时长 25s，速度从 112 降到 81）
  - 菜单态：lap=65535（GT7 在非比赛态给的 0xFFFF）
  - 真实圈：112~157s，速度有起落，峰值 300+ km/h
"""
import json

import pytest

from gt7analysis import (analyze_compare, car_name_of, clean_laps,
                         lap_distances, lap_is_complete, split_laps)


def _lap(lap, n, base_spd, t0, brake_zone=None):
    """一帧序列。速度按「起步加速 → 巡航 → 刹车区 → 巡航」起伏，
    距离才接近真实赛道（有 braking zone 才会切出多段赛车线）。"""
    frames = []
    dist = 0.0
    for i in range(n):
        dt = 1 / 60
        spd = base_spd
        brake = 0.0
        if brake_zone and brake_zone[0] <= dist < brake_zone[1]:
            spd = base_spd * 0.6
            brake = 0.9
        v = spd / 3.6
        dist += v * dt
        # 坐标随距离展开成缓弯（不是纯直线），更接近真实赛车线
        ang = dist / 900.0
        frames.append({
            "t": t0 + i * dt, "lap": lap, "speed_kph": spd,
            "rpm": 6000.0 if spd else 900.0,
            "throttle": 0.0 if brake else 0.6, "brake": brake,
            "g_force": [-0.5 if brake else 0.2, 0.3, 0.0],
            "car_x": 100.0 + 900.0 * (ang if brake_zone else dist / 40.0),
            "car_z": 50.0 + 250.0 * (1 - (0.5 if brake_zone else 1.0)),
        })
    return frames


def real_like_race():
    """复刻 20261007_174306：4 圈真实 + 前圈 + 末圈 + 菜单态。

    实测特征：真实圈距离约 5400~5800m（高度一致），末圈仅 310m。
    """
    frames = []
    t = 1000.0
    frames += _lap(0, 1500, 0.0, t)           # 前圈：566s 几乎全静止
    t += 1500 / 60
    for ln in (1, 2, 3, 4):
        frames += _lap(ln, 7200, 300.0, t, brake_zone=(6000, 9000))
        t += 7200 / 60
    frames += _lap(5, 750, 90.0, t)            # 末圈：25s 滑行离场
    t += 750 / 60
    frames += _lap(65535, 1, 80.0, t)          # 菜单态
    return frames


class TestCleanLaps:
    def test_drop_lead_lap_stalled(self):
        """前圈（静止起步）必须被剔除。"""
        laps = clean_laps(real_like_race())
        assert 0 not in laps

    def test_drop_post_lap_coasting(self):
        """末圈（完赛松油滑行）必须被剔除。"""
        laps = clean_laps(real_like_race())
        assert 5 not in laps

    def test_drop_menu_lap_65535(self):
        """菜单态 0xFFFF 必须剔除。"""
        laps = clean_laps(real_like_race())
        assert not [n for n in laps if n >= 65000]

    def test_keep_real_laps(self):
        """四个真实圈都要留下。"""
        laps = clean_laps(real_like_race())
        assert set(laps) == {1, 2, 3, 4}

    def test_split_laps_unchanged_keeps_raw(self):
        """split_laps 保持旧语义（不剔假圈），向后兼容。"""
        raw = split_laps(real_like_race())
        assert 5 in raw and 65535 in raw

    def test_middle_slow_lap_never_dropped(self):
        """中间圈即使跑得慢（事故/慢速）也不剔除——只剔首尾。"""
        frames = (_lap(1, 7200, 300.0, 1000.0, brake_zone=(6000, 9000))
                  + _lap(2, 7200, 60.0, 1120.0, brake_zone=(6000, 9000))
                  + _lap(3, 7200, 300.0, 1240.0, brake_zone=(6000, 9000)))
        assert set(clean_laps(frames)) == {1, 2, 3}

    def test_single_lap_not_dropped(self):
        """只有一圈时首尾同一圈，不能剔成空。"""
        assert set(clean_laps(_lap(1, 7200, 300.0, 1000.0))) == {1}

    def test_empty_input(self):
        assert clean_laps([]) == {}


class TestLapCompleteness:
    """圈完整性判据（距离口径，P0-1）。

    阈值来自 6 场 57 圈的真实分布：残圈 3.4%~61.2%、完整圈 93.4%~117%、
    时钟跳变假圈 149%~362%。下界 0.90 落在 61→93 的空档，上界 1.25
    落在 117→149 的空档。
    """

    @pytest.mark.parametrize("ratio,want", [
        (0.34, False),      # 刚进圈
        (0.61, False),      # 中途退出（真实数据里的那个假最快圈）
        (0.89, False),      # 差一个弯
        (0.90, True),       # 下界本身算完整
        (0.934, True),      # 实测最短的合法圈（切弯，帧率正常）
        (1.00, True),
        (1.17, True),       # 滚动发车的出场圈
        (1.25, True),       # 上界本身算完整
        (1.26, False),      # 超过上界
        (1.49, False),      # 休眠后的时钟跳变
        (3.62, False),
    ])
    def test_boundaries(self, ratio, want):
        assert lap_is_complete(ratio * 1000.0, 1000.0) is want

    def test_unknown_median_keeps_lap(self):
        """中位数不可用（圈数 <3）时判不了 → 不误杀。"""
        assert lap_is_complete(100.0, 0.0) is True

    def test_median_needs_three_laps(self):
        two = split_laps(_lap(1, 100, 100.0, 0.0) + _lap(2, 100, 100.0, 10.0))
        _, med = lap_distances(two)
        assert med == 0.0

    def test_reference_survives_half_outliers(self):
        """4 圈里 2 个时钟跳变 + 1 个碎片：基准不能被抬到 124%。

        真实场次 20261021_223244 的形状：7173m（正常）/ 10709m（1.5 圈）/
        21823m（3 圈）/ 243m（碎片）。普通中位数 = 8941m，会把正常的
        7173m 压成 80% 判成残圈 —— 所以基准要取「最大同长簇」，
        没有簇时取非碎片里最短的。
        """
        t = 1000.0
        frames = _lap(1, 7200, 300.0, t)          # 基准 ~10000m
        t += 7200 / 60
        frames += _lap(2, 10800, 300.0, t)        # 1.5 倍
        t += 10800 / 60
        frames += _lap(3, 21600, 300.0, t)        # 3 倍
        t += 21600 / 60
        frames += _lap(4, 240, 300.0, t)          # 3% 碎片
        dists, ref = lap_distances(split_laps(frames))
        assert ref == pytest.approx(dists[1], rel=0.02)
        assert lap_is_complete(dists[1], ref) is True
        assert lap_is_complete(dists[2], ref) is False
        assert lap_is_complete(dists[3], ref) is False
        assert lap_is_complete(dists[4], ref) is False

    def test_reference_prefers_largest_cluster(self):
        """正常圈彼此只差 1~2% → 成簇；单个 200% 假圈不该影响基准。"""
        t = 1000.0
        frames = _lap(1, 7200, 300.0, t)
        t += 7200 / 60
        frames += _lap(2, 7200, 303.0, t)         # +1%
        t += 7200 / 60
        frames += _lap(3, 7200, 297.0, t)         # -1%
        t += 7200 / 60
        frames += _lap(4, 14400, 300.0, t)        # 200%
        dists, ref = lap_distances(split_laps(frames))
        assert ref == pytest.approx(dists[1], rel=0.02)
        assert lap_is_complete(dists[4], ref) is False

    def test_partial_first_lap_70pct_is_dropped(self):
        """录制从一圈的 30% 处开始 → 首圈只有 70%，必须剔除。

        旧判据是「<45% 中位数」，70% 会被保留，进而成为最快圈。
        """
        t = 1000.0
        frames = _lap(1, 5040, 300.0, t)          # 7200 帧的 70%
        t += 5040 / 60
        for ln in (2, 3, 4):
            frames += _lap(ln, 7200, 300.0, t)
            t += 7200 / 60
        assert 1 not in clean_laps(frames)

    def test_clock_jump_lap_dropped_when_first(self):
        """首圈距离是中位数的 2 倍（休眠导致两场合并）→ 不能算完整圈。"""
        t = 1000.0
        frames = _lap(1, 14400, 300.0, t)         # 200%
        t += 14400 / 60
        for ln in (2, 3, 4):
            frames += _lap(ln, 7200, 300.0, t)
            t += 7200 / 60
        assert 1 not in clean_laps(frames)

    def test_middle_partial_lap_is_kept(self):
        """中间圈即使只有 55% 也不从分组里删——它可能是一次真实的事故圈，
        由分析层标 incomplete，而不是从数据里消失。"""
        t = 1000.0
        frames = _lap(1, 7200, 300.0, t)
        t += 7200 / 60
        frames += _lap(2, 3960, 300.0, t)         # 55%
        t += 3960 / 60
        frames += _lap(3, 7200, 300.0, t)
        assert set(clean_laps(frames)) == {1, 2, 3}

    @staticmethod
    def _race_with_partial_middle():
        """4 圈：第 2 圈只跑了 55%（66s，时长 >20s，帧数也够）。"""
        t = 1000.0
        frames = _lap(1, 7200, 300.0, t)
        t += 7200 / 60
        frames += _lap(2, 3960, 300.0, t)
        t += 3960 / 60
        frames += _lap(3, 7200, 300.0, t)
        t += 7200 / 60
        frames += _lap(4, 7200, 300.0, t)
        return frames

    def test_partial_middle_not_in_compare(self):
        """中间残圈不能被选成参考圈，也不能出现在圈速摘要里。"""
        r = analyze_compare(self._race_with_partial_middle())
        assert r["ref_lap"] != 2
        assert 2 not in [s["lap"] for s in r["lap_summary"]]

    def test_analyze_session_marks_partial_middle(self, dash, tmp_path):
        """详情页圈速表要把中间残圈标成 incomplete，且不能当最快圈。"""
        p = tmp_path / "20261021_120000_T.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"session_id": "x"}) + "\n")
            for fr in self._race_with_partial_middle():
                f.write(json.dumps(fr, separators=(",", ":")) + "\n")
        st = dash.analyze_session(p)
        rows = {l["lap"]: l for l in st["laps"]}
        assert rows[2]["incomplete"] is True
        assert rows[2]["dist_m"] < rows[1]["dist_m"] * 0.8
        assert st["best_lap"]["lap"] in (1, 3, 4)


class TestRefLapSelection:
    def test_default_ref_is_fastest_real_lap(self):
        """默认参考圈 = 最快真实圈，不是 25s 的离场末圈。"""
        r = analyze_compare(real_like_race())
        assert r["ref_lap"] == 1
        assert r["laps_analyzed"] == 4

    def test_fake_laps_never_chosen_as_ref(self):
        """即使真实圈很慢（但跑完整圈），参考圈也必在真实圈内，
        绝不会选到前圈/末圈/菜单圈。"""
        frames = real_like_race()
        # 把真实圈整体放慢（但仍跑完整距离），假圈相对更快
        for f in frames:
            if f["lap"] in (1, 2, 3, 4):
                f["speed_kph"] = 30.0
        r = analyze_compare(frames)
        assert r["ref_lap"] in (1, 2, 3, 4)
        assert r["ref_lap"] not in (0, 5, 65535)

    def test_explicit_ref_lap_honored(self):
        frames = real_like_race()
        for want in (2, 3, 4):
            r = analyze_compare(frames, ref_lap_no=want)
            assert r["ref_lap"] == want

    def test_race_line_has_many_segments(self):
        """真实圈应画出成段的赛道线，而不是离场的单段直线。"""
        r = analyze_compare(real_like_race())
        assert len(r["race_line"]["segments"]) > 1

    @pytest.mark.parametrize("bad", [9, 0, -5, 99999])
    def test_invalid_ref_lap_falls_back(self, bad):
        """URL 被手改成非法圈号时回退最快圈，不抛异常。"""
        r = analyze_compare(real_like_race(), ref_lap_no=bad)
        assert r["ref_lap"] == 1

    def test_empty_frames_no_crash(self):
        r = analyze_compare([])
        assert r["laps_analyzed"] == 0 and r["ref_lap"] is None


class TestCarName:
    def test_lookup_hit(self, tmp_path):
        csv = tmp_path / "cars.csv"
        csv.write_text("ID,ShortName,Maker\n3459,918 Spyder '13,136\n",
                       encoding="utf-8")
        assert car_name_of(3459, str(csv)) == "918 Spyder '13"

    def test_lookup_miss_keeps_id(self, tmp_path):
        csv = tmp_path / "cars.csv"
        csv.write_text("ID,ShortName,Maker\n1,Car One,2\n", encoding="utf-8")
        assert car_name_of(999, str(csv)) == "CAR-ID-999"

    def test_zero_code_is_empty(self, tmp_path):
        assert car_name_of(0, str(tmp_path / "nope.csv")) == ""

    def test_missing_csv_no_crash(self, tmp_path):
        assert car_name_of(3459, str(tmp_path / "absent.csv")) == "CAR-ID-3459"

    def test_extra_table_covers_community_gap(self, tmp_path):
        """社区表还没收录的车（如 3656）由手工补充表补上。"""
        csv = tmp_path / "cars.csv"
        csv.write_text("ID,ShortName,Maker\n3459,918 Spyder '13,136\n",
                       encoding="utf-8")
        (tmp_path / "cars_extra.csv").write_text(
            "ID,ShortName,Maker\n3656,Xiaomi Vision Gran Turismo,\n",
            encoding="utf-8")
        # 补充表与主表同目录 → 自动生效，不用改调用处
        assert car_name_of(3656, str(csv)) == "Xiaomi Vision Gran Turismo"
        # 主表该有的照旧
        assert car_name_of(3459, str(csv)) == "918 Spyder '13"

    def test_extra_table_overrides_community(self, tmp_path):
        """补充表优先：玩家自己标的名字覆盖社区表的（万一社区译名不合意）。"""
        csv = tmp_path / "cars.csv"
        csv.write_text("ID,ShortName,Maker\n3459,918 Spyder '13,136\n",
                       encoding="utf-8")
        (tmp_path / "cars_extra.csv").write_text(
            "ID,ShortName,Maker\n3459,保时捷 918,\n", encoding="utf-8")
        assert car_name_of(3459, str(csv)) == "保时捷 918"