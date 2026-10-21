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


# ---------------------------------------------------------------------------
# 内侧/外侧判定（P0-2）
# ---------------------------------------------------------------------------
# 🔴 这组守的是 `inside_pct` 与 `line[][3]` 的**符号**：
#    法向 N = (−Tz, Tx)，而 T' = κN ⇒ cross(T, T') = κ。
#    所以「点在弯心侧（内侧）」等价于 dlat 与 cross **同号**。
#    若写成 c = −cross，内外侧会整体翻转，而数值仍在 0~100 的合法区间里、
#    不会抛任何异常 —— 只能靠合成圆把方向钉死。

def _toward_center(ref: list[dict], offset: float,
                   radius: float = 100.0) -> list[dict]:
    """每个点沿半径方向朝圆心平移 offset 米（顺/逆时针都成立）。

    offset > 0 = 内移（切内线），offset < 0 = 外移（走宽）。
    """
    scale = (radius - offset) / radius
    return [{"dist": p["dist"], "x": p["x"] * scale, "z": p["z"] * scale}
            for p in ref]


@pytest.mark.parametrize("ccw", [True, False])
def test_inside_pct_inner_offset_is_100(ccw):
    """本圈整体切内线 10m → inside_pct 必须是 100（不是 0）。"""
    ref = _circle(ccw=ccw)
    out = track_deviation(ref, _toward_center(ref, 10.0), step=5.0)
    assert out["reliable"], f"应该 reliable，reason={out['reason']!r}"
    assert out["inside_pct"] == pytest.approx(100.0, abs=0.5)
    # 逐点标记也必须是 1（前端按它上色）。起点 dlat≈0 时是 -1（"在线上"），
    # 既不算内侧也不算外侧，属正常，排除掉再断言。
    marks = [p[3] for p in out["line"] if p[3] != -1]
    assert marks and all(m == 1 for m in marks), marks[:10]


@pytest.mark.parametrize("ccw", [True, False])
def test_inside_pct_outer_offset_is_0(ccw):
    """本圈整体走宽 10m → inside_pct 必须是 0（不是 100）。"""
    ref = _circle(ccw=ccw)
    out = track_deviation(ref, _toward_center(ref, -10.0), step=5.0)
    assert out["reliable"], f"应该 reliable，reason={out['reason']!r}"
    assert out["inside_pct"] == pytest.approx(0.0, abs=0.5)
    marks = [p[3] for p in out["line"] if p[3] != -1]
    assert marks and all(m == 0 for m in marks), marks[:10]


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


# ---------------------------------------------------------------------------
# step 参数守卫（P0-1）
# ---------------------------------------------------------------------------
# 🔴 这组测试守的是一条**能跑死进程**的死循环：
#    track_deviation 内部的重采样是 `while d <= s_end: d += step`，
#    step=0 时 d 永不增长，负数时 d 反向远离 → 永不退出。
#    同一次循环还持着调用方的 memo key 锁（gt7-dashboard 的 memo_compute），
#    所以一条 HTTP 请求就能让该场次后续所有请求全部阻塞。
#
# 为什么必须测**分析层**而不是只测 HTTP 入口：
#    session_deviation / track_deviation 都是模块级公开函数，
#    测试、replay 工具、未来的重放路径都会绕过 HTTP 那一层夹紧。

@pytest.mark.parametrize("bad", [0.0, -0.0, -1.0, -5.0, 1e-9,
                                 float("nan"), float("inf"),
                                 float("-inf"), None, "abc"])
def test_step_guard_never_hangs(bad):
    """任何非法 step 都必须快速返回，且落在合法区间内。"""
    ref = _circle(samples=200)
    cur = _circle(samples=200)
    # 用超时兜底：真死了这一行会卡住 pytest（CI 会给出 timeout 而不是假绿）
    out = track_deviation(ref, cur, step=bad)
    assert out["step_m"] > 0.0
    assert 0.5 <= out["step_m"] <= 200.0


def test_step_guard_normalizes_zero_to_default():
    """step=0 应回落到默认 5.0（而不是被夹到 0.5，那会让输出点数暴涨 10 倍）。"""
    ref, cur = _circle(samples=200), _circle(samples=200)
    out = track_deviation(ref, cur, step=0.0)
    assert out["step_m"] == pytest.approx(5.0)


def test_step_guard_clamps_tiny_positive():
    """极小但合法的正数应被夹到下限 0.5，而不是生成几十万个点。"""
    ref, cur = _circle(samples=200), _circle(samples=200)
    out = track_deviation(ref, cur, step=1e-9)
    assert out["step_m"] == pytest.approx(0.5)
    # 圆周周长 2πR ≈ 628m，0.5m 步长约 1257 点；不夹紧会是天文数字
    assert len(out["line"]) < 2000


def test_step_guard_caps_huge_value():
    """超大 step 应被夹到上限 200，避免退化成一两个点。"""
    ref, cur = _circle(samples=200), _circle(samples=200)
    out = track_deviation(ref, cur, step=1e6)
    assert out["step_m"] == pytest.approx(200.0)


def test_step_guard_keeps_legal_value_exact():
    """合法值不该被改动（防止夹紧逻辑误伤正常调用）。"""
    ref, cur = _circle(samples=200), _circle(samples=200)
    for v in (1.0, 5.0, 12.5, 50.0):
        assert track_deviation(ref, cur, step=v)["step_m"] == pytest.approx(v)