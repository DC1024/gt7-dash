"""行车轨迹双着色口径 + 圈间对比自选参考圈/对比圈。

背景（用户反馈）：
1. 历史场次里原来只有「参考圈赛车线（按踏板着色）」，要求改名为「行车轨迹」
   并像实时仪表盘一样支持「按 G 力着色」——所以 race_line 的每段要同时带
   踏板开度 b[]/t[] 与合成 G 大小 g[]。
2. 「圈间对比分析」原来只能拿最后一圈跟参考圈比，要求参考圈与对比圈都能自选。
"""

import re
from pathlib import Path

import pytest

from gt7analysis import (analyze_compare, clean_laps, lap_samples,
                         race_line, race_line_of_lap)

ROOT = Path(__file__).resolve().parents[1]


def make_lap_frames(lap, n=600, spd_kph=150.0, g=(0.2, 0.3), brake_zone=None):
    """合成一圈：直线行驶（车_x 递增），可选刹车区间与恒定 G。"""
    frames = []
    t0 = 1000.0 + lap * 100.0
    dist = 0.0
    for i in range(n):
        dt = 1 / 60
        spd = spd_kph
        throttle, brake = 0.6, 0.0
        if brake_zone and brake_zone[0] <= dist < brake_zone[1]:
            spd = spd_kph * 0.6
            throttle, brake = 0.0, 0.9
        dist += spd / 3.6 * dt
        frames.append({
            "t": t0 + i * dt, "lap": lap, "speed_kph": spd,
            "throttle": throttle, "brake": brake,
            "g_force": [g[0], g[1], 0.0],
            "car_x": 100 + dist, "car_z": 50.0,
        })
    return frames


# ---------- 采样点带上合成 G 大小 ----------

class TestGmag:
    def test_lap_samples_carries_gmag(self):
        pts = lap_samples(make_lap_frames(1, 60, g=(3.0, 4.0)))
        assert all("gmag" in p for p in pts)
        # hypot(3,4) = 5 —— 横向 + 纵向合成
        assert pts[0]["gmag"] == pytest.approx(5.0, abs=1e-3)

    def test_missing_g_force_defaults_zero(self):
        pts = lap_samples([{"t": 1.0, "lap": 1, "speed_kph": 100.0,
                            "car_x": 0.0, "car_z": 0.0}])
        assert pts[0]["gmag"] == 0.0


# ---------- race_line 同时带两套口径 ----------

class TestRaceLineBothChannels:
    def test_g_array_aligned_with_points(self):
        """和 b[]/t[] 一样，g[] 也必须与 pts 一一对齐，前端才能逐点插值。"""
        pts = lap_samples(make_lap_frames(1, 1200, 180.0,
                                          brake_zone=(300, 500)))
        segs = race_line(pts)["segments"]
        assert segs
        for s in segs:
            assert len(s["g"]) == len(s["pts"]) == len(s["b"]) == len(s["t"])
            assert all(v >= 0.0 for v in s["g"]), "合成 G 大小不可能为负"

    def test_g_keeps_boundary_point(self):
        """换色处首尾相接的老约定，对 g[] 一样成立（否则 G 线会断）。"""
        pts = lap_samples(make_lap_frames(1, 1200, 180.0,
                                          brake_zone=(300, 500)))
        segs = race_line(pts)["segments"]
        for a, b in zip(segs, segs[1:]):
            assert a["g"][-1] == b["g"][0]


# ---------- 单圈赛车线（行车轨迹卡片切圈用） ----------

class TestRaceLineOfLap:
    def _frames(self):
        return (make_lap_frames(1, 600, 180.0, g=(0.2, 0.3))
                + make_lap_frames(2, 600, 175.0, g=(1.5, 0.1)))

    def test_returns_requested_lap(self):
        r = race_line_of_lap(self._frames(), 2)
        assert r["lap"] == 2
        assert r["segments"], "第 2 圈应该有轨迹"
        # 第 2 圈 G 更大，抽一点验口径确实是第 2 圈的
        gs = [v for s in r["segments"] for v in s["g"]]
        assert max(gs) > 1.0, "第 2 圈 G 高（hypot(1.5,0.1)≈1.5）"

    def test_different_laps_give_different_data(self):
        a = race_line_of_lap(self._frames(), 1)["segments"]
        b = race_line_of_lap(self._frames(), 2)["segments"]
        ga = max(v for s in a for v in s["g"])
        gb = max(v for s in b for v in s["g"])
        assert ga < gb, "两圈数据不同，取到的线也必须不同"

    def test_unknown_lap_is_empty_not_crash(self):
        r = race_line_of_lap(self._frames(), 99)
        assert r == {"lap": 99, "segments": []}

    def test_uses_clean_laps_semantics(self):
        """圈号口径必须跟 clean_laps 一致（菜单态 65535 圈拿不到数据）。"""
        frames = self._frames() + [{"t": 9e9, "lap": 65535, "speed_kph": 0,
                                    "car_x": 0.0, "car_z": 0.0}]
        assert race_line_of_lap(frames, 65535)["segments"] == []
        assert 65535 not in clean_laps(frames)

    def test_degenerate_lap_no_crash(self):
        assert race_line_of_lap([], 1)["segments"] == []
        assert race_line_of_lap([{"t": 1.0, "lap": 1}], 1)["segments"] == []


# ---------- 参考圈 / 对比圈都能自选 ----------

class TestCompareLapSelection:
    def _frames(self):
        # 三圈，故意让「最后一圈」不是「最快圈」
        return (make_lap_frames(1, 600, 180.0) + make_lap_frames(2, 600, 190.0)
                + make_lap_frames(3, 600, 175.0))

    def test_default_unchanged(self):
        """不传参数时行为不变：参考=最快圈，对比=最后一圈。"""
        r = analyze_compare(self._frames())
        assert r["ref_lap"] == 1        # 圈 1 距离最短 → 最快
        assert r["cur_lap"] == 3        # 最后一圈

    def test_explicit_cmp_lap(self):
        r = analyze_compare(self._frames(), ref_lap_no=1, cur_lap_no=2)
        assert r["ref_lap"] == 1 and r["cur_lap"] == 2

    def test_explicit_both_can_be_non_extremes(self):
        r = analyze_compare(self._frames(), ref_lap_no=2, cur_lap_no=1)
        assert r["ref_lap"] == 2 and r["cur_lap"] == 1

    def test_invalid_cmp_lap_falls_back(self):
        """URL 可能被手改成任意值，不能 KeyError 把详情页打挂。"""
        r = analyze_compare(self._frames(), ref_lap_no=1, cur_lap_no=99)
        assert r["cur_lap"] == 3

    def test_same_lap_as_ref_falls_back_to_another(self):
        """参考圈与对比圈撞一起时不能自己跟自己比（时间差会全 0，没意义）。"""
        r = analyze_compare(self._frames(), ref_lap_no=2, cur_lap_no=2)
        assert r["cur_lap"] != r["ref_lap"]
        assert r["cur_lap"] in (1, 3)

    def test_single_lap_keeps_same(self):
        """只有一圈时没有别的圈可退，保持原样而不是报错。"""
        r = analyze_compare(make_lap_frames(1, 600, 180.0),
                            ref_lap_no=1, cur_lap_no=1)
        assert r["ref_lap"] == r["cur_lap"] == 1

    def test_race_line_follows_ref_lap(self):
        """行车轨迹画的是参考圈的线（改参考圈，线也要跟着换）。"""
        r = analyze_compare(self._frames(), ref_lap_no=2)
        g2 = max(v for s in r["race_line"]["segments"] for v in s["g"])
        r3 = analyze_compare(self._frames(), ref_lap_no=3)
        g3 = max(v for s in r3["race_line"]["segments"] for v in s["g"])
        assert g2 == g3 == pytest.approx(0.3606, abs=0.01)   # hypot(.2,.3)


# ---------- 两套配色在实时页 / 历史页必须逐字一致 ----------

def _js_span(src: str, name: str, at: int = 0) -> tuple[str, int]:
    """从 src[at:] 里抠函数体，返回 (函数体, 函数结束后的下标)。

    🔴 别用 `\\{(.*?)\\n\\}`：函数体里带缩进的 `}` 不是行首，非贪婪会一路
       吃到最外层 IIFE 的收尾，把后面半个页面的 JS 都算进来（实测踩过）。
    """
    m = re.search(r"function\s+" + name + r"\s*\([^)]*\)\s*\{", src[at:])
    assert m, "找不到函数 " + name
    i = src.index("{", at + m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j], j + 1
    raise AssertionError("函数 " + name + " 的花括号不闭合")


def _js_body(src: str, name: str) -> str:
    return _js_span(src, name)[0]


def _js_bodies(src: str, name: str) -> list[str]:
    """同名函数在文件里出现了几次（实时页 / 详情页各一份）。"""
    out: list[str] = []
    at = 0
    while True:
        try:
            body, at = _js_span(src, name, at)
        except AssertionError:
            return out
        out.append(body)


class TestColorSync:
    def _dash(self):
        return (ROOT / "gt7-dashboard.py").read_text(encoding="utf-8")

    def test_gcolor_identical_in_both_pages(self):
        """实时仪表盘的 gColor 与历史详情页的 gColor 必须一模一样。

        两份是同一文件里不同 page shell 的 script 块，没法共享常量，
        只能靠这条测试拦住「改了一边忘了另一边」。
        """
        bodies = _js_bodies(self._dash(), "gColor")
        assert len(bodies) == 2, "应该正好有两份 gColor，实际 %d" % len(bodies)
        norm = [re.sub(r"\s+", " ", b).strip() for b in bodies]
        assert norm[0] == norm[1], "两页的 G 力配色实现已不同步"

    def test_gcolor_anchors_stable(self):
        """配色锚点变了要显式确认（蓝 → 绿 → 橙 → 红，量程 0~3g）。

        四段端点：rgb(13,110,253) → rgb(25,135,84) → rgb(253,126,20)
                  → rgb(220,53,69)，按通道各写一次 lerp。
        """
        b = re.sub(r"\s+", " ", _js_body(self._dash(), "gColor"))
        for anchor in ["/ 3", "lerp(13, 25, k)", "lerp(110, 135, k)",
                       "lerp(253, 84, k)", "lerp(25, 253, k)",
                       "lerp(135, 126, k)", "lerp(84, 20, k)",
                       "lerp(253, 220, k)", "lerp(126, 53, k)",
                       "lerp(20, 69, k)"]:
            assert anchor in b, "G 配色锚点缺失：" + anchor

    def test_pedal_anchors_match_between_pages(self):
        """踏板渐变的四个锚点在两个页面里必须一致。"""
        src = self._dash()
        for a, n in (("255, 105, 180", "粉"), ("255, 40, 40", "红"),
                     ("0, 188, 212", "青"), ("0, 200, 83", "绿")):
            assert src.count(a) >= 2, ("%s（%s）只出现一次，两页配色可能不同步"
                                       % (a, n))
