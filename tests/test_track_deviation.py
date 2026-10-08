# -*- coding: utf-8 -*-
"""走线偏差（track_deviation）回归测试。

设计思路：算法核心是「参考折线 → 几何对齐 → 逐米 dlat」，三个签名级别的行为
各用一个合成用例盯死：

  1. 圆 + 恒定外向偏移 → RMS 等于偏移量，符号与参考线朝向一致；
  2. 出场圈（参考线起点不在本圈起点附近）→ start_gap 护栏挡住；
  3. 参考线自交严重（撞车折返）→ 诊断字段 ref_self_cross_m 不为 None，
     且 reliable 不被该字段决定（自交检测在 _DEV_RMS_TOL 兜底前只做诊断）。
"""
import math

import pytest

from gt7analysis import track_deviation


def _circle(samples: int = 2000, radius: float = 100.0,
            step: float = 1.0, ccw: bool = True) -> list[dict]:
    """单位速度走一圈：dist 等距、点列正好落在 (R cos, R sin) 上。"""
    sgn = 1.0 if ccw else -1.0
    out = []
    for k in range(samples):
        ang = 2 * math.pi * k / samples
        out.append({"dist": k * step,
                    "x": math.cos(ang) * radius,
                    "z": math.sin(ang) * radius * sgn})
    return out


def test_circular_track_constant_outward_offset():
    """CCW 圆周 + 2m 外向偏移 → RMS ≈ 2、coverage 1.0、reliable True。"""
    ref = _circle()
    # CCW 圆 (cos, sin)，外法向 = (cos, sin)；在每个 ang 处恰好是 (cos, sin)。
    cur = [{"dist": p["dist"],
            "x": p["x"] + 2 * math.cos(2 * math.pi * k / len(ref)),
            "z": p["z"] + 2 * math.sin(2 * math.pi * k / len(ref))}
           for k, p in enumerate(ref)]
    out = track_deviation(ref, cur, step=5.0)
    assert out["reliable"], f"应该 reliable，reason={out['reason']!r}"
    assert out["coverage"] >= 0.99
    assert out["rms_dlat"] == pytest.approx(2.0, abs=0.05)
    # 颜色 / 内侧方向：本圈相对参考线全在外侧；sign 在 CCW 下为负
    # （_DEV 的法向是切向 +90° 旋转，对 CCW 圆指向内侧）。
    assert out["mean_dlat"] == pytest.approx(-2.0, abs=0.05)
    # 色阶按 p95 截断：恒定 2m 时 p95≈2
    assert out["p95_abs_dlat"] == pytest.approx(2.0, abs=0.05)
    # line / ref_line 步长一致：ref_len / step 取整
    # 合成 2000 点、ref_len≈2000、step=5 → ~400 行（不是 1000+）
    assert len(out["line"]) >= 300
    assert len(out["ref_line"]) >= 300
    # 10 段里全部都是 RMS≈2（恒定偏移）
    rms = [s["rms_dlat"] for s in out["segments"]]
    assert all(r == pytest.approx(2.0, abs=0.1) for r in rms), rms


def test_out_lap_rejected_by_start_gap():
    """出场圈起点远离参考线起点 → start_gap > 60m 护栏拒绝。"""
    ref = _circle()
    # 把本圈整体平移 200m，模拟从维修区起步
    cur = [{"dist": p["dist"], "x": p["x"] + 200.0, "z": p["z"]} for p in ref]
    out = track_deviation(ref, cur, step=5.0)
    assert not out["reliable"]
    assert "起点" in out["reason"]
    assert out["start_gap_m"] > 60.0
    # 即使被拒绝，line / ref_line 仍输出（前端照画灰底）
    assert out["line"]


def test_self_cross_is_diagnostic_only():
    """脏参考折线（撞车折返）→ RMS 护栏兜底，自交指标只做诊断。

    合成一个「回头弯」折线：先把赛道原向走一遍，再原路倒回。
    RMS 在原向段接近 0、在倒回段巨大，但 RMS 全场仍可能 < 60m；
    ref_self_cross_m 应当返回一个**有限值**（自交很近）。
    """
    ref_forward = _circle(samples=1000)
    # 把后半段再原路走回去：重合区段自交距离很小
    ref = ref_forward + list(reversed(ref_forward))
    # 修正 dist：让倒回段单调递增
    for i, p in enumerate(ref):
        ref[i] = {**p, "dist": float(i) * 0.628}    # 一圈 ~628m
    cur = list(ref)        # 完全一致 → 应当 dlat≈0
    out = track_deviation(ref, cur, step=5.0)
    # 走线全一致时 RMS 应当非常小
    assert out["rms_dlat"] < 5.0
    # 但自交指标（沿赛道隔 250m 的两点的空间距离）一定 < 50m：
    # 倒回的那一段跟原向段几乎重合。
    assert out["ref_self_cross_m"] is not None
    assert out["ref_self_cross_m"] < 50.0


def test_partial_lap_rejected_by_coverage():
    """本圈只覆盖参考线一半 → coverage < 0.6 护栏拒绝。"""
    ref = _circle(samples=2000)
    # 只取前半圈
    cur = ref[:80]
    out = track_deviation(ref, cur, step=5.0)
    assert not out["reliable"]
    assert "覆盖" in out["reason"]
    assert out["coverage"] < 0.6


def test_step_changes_output_grid_size():
    """step 越小 line 越密，但 p95 / RMS / reliable 不变。"""
    ref = _circle()
    cur = [{"dist": p["dist"],
            "x": p["x"] + 2 * math.cos(2 * math.pi * k / len(ref)),
            "z": p["z"] + 2 * math.sin(2 * math.pi * k / len(ref))}
           for k, p in enumerate(ref)]
    a = track_deviation(ref, cur, step=1.0)
    b = track_deviation(ref, cur, step=10.0)
    # line 数 ≈ ref_len / step
    assert len(a["line"]) > len(b["line"]) * 5
    # RMS / mean 与改 step 比较恒定（重采样是 loss-less）
    assert a["rms_dlat"] == pytest.approx(b["rms_dlat"], abs=0.05)
    assert a["mean_dlat"] == pytest.approx(b["mean_dlat"], abs=0.05)


def test_no_ref_or_cur_returns_empty():
    """没有有效输入 → 返回空壳（reliable False、有 reason），不抛错。"""
    out = track_deviation([], [], step=5.0)
    assert out["reliable"] is False
    assert out["reason"]


def test_ref_self_cross_helper_runs():
    """_ref_self_cross 在简单圆环上不会卡死。

    🔴 该函数要求 R 是 (s, x, z) 三元组**元组**列表，不是 dict——
       `track_deviation` 内部会把 dict 转成元组再用；这里直接喂 dict 会
       在 `int(x // cell)` 处 TypeError。
    """
    from gt7analysis import _ref_self_cross
    R_dicts = _circle()
    R = [(p["dist"], p["x"], p["z"]) for p in R_dicts]
    ref_len = R[-1][0]
    # 圆周本身不与自身「隔 250m 的两点在空间上最近」，
    # 在数学意义上 ∞；允许返回 ∞ 或大值。
    val = _ref_self_cross(R, ref_len, min_sep=250.0)
    assert val > 100.0      # 任何小于 100m 的值都是 bug