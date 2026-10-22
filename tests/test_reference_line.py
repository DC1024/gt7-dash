# -*- coding: utf-8 -*-
"""E2 参考走线：单圈重采样 + 多圈中位数。

为什么要测这两件事：
  · **重采样按弧长而不是按帧** —— 60Hz 下低速弯的帧密度是直道的几十倍，
    按帧采样会让弯道吃掉大部分点数，直道被压成一条线。
  · **中位数而不是平均** —— 某一场冲出赛道/打转时，平均会被整体拉偏，
    参考线就不再是"你通常怎么跑"了。
"""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gt7analysis as ga


def _circle_frames(n=600, radius=100.0, t0=0.0, dt=0.02,
                   cx=0.0, cz=0.0, ccw=True):
    """一整圈的圆形轨迹帧。周长 = 2πR。"""
    out = []
    for i in range(n):
        ang = 2 * math.pi * i / n * (1 if ccw else -1)
        out.append({"t": t0 + i * dt,
                    "car_x": cx + radius * math.cos(ang),
                    "car_z": cz + radius * math.sin(ang)})
    return out


class TestLapLine:
    def test_圆形轨迹_点数与弧长正确(self):
        r = 100.0
        frames = _circle_frames(600, r)
        out = ga.lap_line(frames, n_points=120)
        assert "error" not in out, out
        assert out["n_points"] == 120
        assert len(out["pts"]) == 120
        # 重采样是"n 等分、不含终点"，折线长度略小于整圆周长
        circ = 2 * math.pi * r
        assert 0.95 * circ <= out["len_m"] <= 1.02 * circ, out["len_m"]

    def test_点按弧长等距_而不是按帧(self):
        """构造"一半直道 + 一半密集低速弯"的轨迹。

        按帧采样的结果里，密集那段会占掉大部分点；按弧长则各占一半。
        """
        pts = []
        # 直道：1000m，20 个点（每 50m 一个）
        for i in range(20):
            pts.append({"t": i * 0.02, "car_x": float(i * 50), "car_z": 0.0})
        # 密集段：沿 x 再走 1000m，但用 2000 个点（模拟低速弯的帧密度）
        for i in range(2000):
            pts.append({"t": 1.0 + i * 0.001,
                        "car_x": 950.0 + i * 0.5, "car_z": 0.0})
        out = ga.lap_line(pts, n_points=120)
        assert "error" not in out
        xs = [p[0] for p in out["pts"]]
        # 按弧长等距 → x 大致线性增长；按帧 → 前 1% 覆盖一半，后面挤在一起
        gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        assert max(gaps) - min(gaps) < 20.0, (
            f"点间距不均匀（{min(gaps):.1f}~{max(gaps):.1f}m）—— "
            "说明不是按弧长采样")

    def test_坐标跳变被跳过且不计入弧长(self):
        """跳变段必须整段丢弃：只丢点、把跳变算进弧长，后面的点会全部错位。"""
        pts = [{"t": i * 0.02, "car_x": float(i), "car_z": 0.0}
               for i in range(100)]
        # 瞬移 5000m（主机休眠）
        pts += [{"t": 2.0 + i * 0.02, "car_x": 5000.0 + i, "car_z": 0.0}
                for i in range(100)]
        out = ga.lap_line(pts, n_points=120)
        assert "error" not in out
        # 两段各 99m，跳变不计 → 总长约 198m（不是 5198m）
        assert out["len_m"] < 300, f"跳变被算进弧长了：{out['len_m']}m"
        assert out["len_m"] > 150, (
            f"跳变之后的点被丢掉了（只剩 {out['len_m']}m，应为 ~198m）—— "
            "这是「continue 跳过所有后续点」的老 bug")
        assert out["n_gaps"] == 1

    def test_缺坐标的帧被忽略(self):
        frames = _circle_frames(600)
        for i in range(0, 600, 3):
            frames[i].pop("car_x")
        out = ga.lap_line(frames)
        assert "error" not in out, out

    def test_点数太少返回错误(self):
        frames = _circle_frames(10)
        out = ga.lap_line(frames)
        assert "error" in out
        assert out["n_raw"] == 10

    def test_空输入不炸(self):
        assert "error" in ga.lap_line([])
        assert "error" in ga.lap_line(None)

    def test_点数被夹进合理区间(self):
        frames = _circle_frames(600)
        assert ga.lap_line(frames, n_points=1)["n_points"] == ga._REF_LINE_MIN_PTS
        assert ga.lap_line(frames, n_points=99999)["n_points"] == 600
        # 非法值回落默认
        assert ga.lap_line(frames, n_points="abc")["n_points"] == \
            ga._REF_LINE_POINTS


class TestReferenceLine:
    def _line(self, shift_x=0.0, radius=100.0, n=120):
        return [[radius * math.cos(2 * math.pi * i / n) + shift_x,
                 radius * math.sin(2 * math.pi * i / n)]
                for i in range(n)]

    def test_完全相同的两条线_参考线等于它们(self):
        a = self._line()
        out = ga.reference_line([a, a])
        assert "error" not in out
        assert out["n_laps"] == 2 and out["n_used"] == 2
        assert out["spread_m"] == 0.0
        for i in (0, 30, 60, 119):
            assert abs(out["pts"][i][0] - a[i][0]) < 0.02
            assert abs(out["pts"][i][1] - a[i][1]) < 0.02

    def test_一条离群线不拉偏中位数(self):
        """三圈：两圈在原位，一圈整体偏移 30m。中位数应贴着原位那两圈。"""
        a = self._line(0.0)
        b = self._line(0.0)
        c = self._line(30.0)
        out = ga.reference_line([a, b, c])
        assert out["n_laps"] == 3
        # 中位数取 (0, 0, 30) 的中间那个 → 0
        assert abs(out["pts"][0][0] - a[0][0]) < 0.5, (
            f"离群线把参考线拉偏了：{out['pts'][0][0]:.2f} vs {a[0][0]:.2f}")

    def test_平均会被拉偏_中位数不会(self):
        """对照：同样的输入，平均值的偏移量明显大于中位数。"""
        a = self._line(0.0)
        b = self._line(0.0)
        c = self._line(30.0)
        med = ga.reference_line([a, b, c])
        avg_x = sum(p[0][0] for p in (a, b, c)) / 3
        assert abs(med["pts"][0][0] - a[0][0]) < 0.5
        assert abs(avg_x - a[0][0]) > 5.0, "这个用例的前提是平均值确实被拉偏了"

    def test_圈长偏离的线被剔除(self):
        """一条圈长多 20% 的线（跑错路线）不参与统计。"""
        a = self._line(0.0)
        b = self._line(0.0)
        # 半径 1.2 倍 → 弧长多 20%
        c = self._line(0.0, radius=120.0)
        out = ga.reference_line([a, b, c])
        assert out["n_laps"] == 3
        assert out["n_used"] == 2, "圈长偏 20% 的线没被剔除"

    def test_点数不一致的线被忽略(self):
        a = self._line()
        b = self._line()[:100]          # 点数不对
        out = ga.reference_line([a, b])
        assert out["n_laps"] == 1, "点数不一致的线不该参与"
        assert out["n_used"] == 1

    def test_单条线也能用(self):
        out = ga.reference_line([self._line()])
        assert "error" not in out
        assert out["n_laps"] == 1 and out["n_used"] == 1
        assert out["spread_m"] == 0.0   # 只有一条，没有离散度

    def test_空输入返回错误(self):
        assert "error" in ga.reference_line([])
        assert "error" in ga.reference_line(None)
        assert "error" in ga.reference_line([[]])

    def test_离散度反映真实差异(self):
        """两圈整体差 4m → spread 应该接近 4m。"""
        a = self._line(0.0)
        b = self._line(4.0)
        out = ga.reference_line([a, b])
        # 中位线在两者中间，各偏 2m
        assert 1.5 < out["spread_m"] < 2.5, out["spread_m"]

    def test_离散度过大是赛道识别错误的信号(self):
        """两圈差 40m（根本不是同一条走线）→ spread 明显大于正常范围。"""
        a = self._line(0.0)
        b = self._line(40.0)
        out = ga.reference_line([a, b])
        assert out["spread_m"] > 10.0, (
            "spread 没反映出这两条线根本不是一回事")


class TestEndToEnd:
    def test_两圈合成参考线_再算偏差接近零(self):
        """端到端：两圈 → 参考线 → 用参考线算这两圈的偏差，应接近 0。"""
        frames1 = _circle_frames(600, 100.0)
        frames2 = _circle_frames(600, 100.0, t0=30.0)
        l1 = ga.lap_line(frames1)["pts"]
        l2 = ga.lap_line(frames2)["pts"]
        ref = ga.reference_line([l1, l2])
        assert "error" not in ref

        cur = [{"car_x": f["car_x"], "car_z": f["car_z"]}
               for f in frames2]
        # track_deviation 的 ref_pts 要 {dist, x, z}，cur_pts 要 {x, z}
        ref_pts = [{"dist": ref["dist_m"][i], "x": p[0], "z": p[1]}
                   for i, p in enumerate(ref["pts"])]
        dev = ga.track_deviation(
            ref_pts, [{"x": f["car_x"], "z": f["car_z"]} for f in frames2])
        assert dev["reliable"], dev.get("reason")
        assert dev["rms_dlat"] < 1.0, (
            f"同一圈的偏差应该接近 0，实际 {dev['rms_dlat']:.2f}m")
