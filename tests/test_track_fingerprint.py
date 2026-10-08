# -*- coding: utf-8 -*-
"""赛道指纹 / 自动识别 —— 回归测试。

合成形状盯死三个签名行为：
  1. 同形状不同平移/尺度/方向 ⇒ 指纹距离 ≈ 0；
  2. 不同形状（圆 vs 长条跑道）⇒ 距离显著大于同形状；
  3. 代表圈选择：脏圈（出赛道）/ 残圈（半圈）不得当选。
"""
import math

import pytest

from gt7analysis import (fingerprint_distance, track_fingerprint,
                         _fp_normalize, _fp_resample)


def _circle_pts(samples=1500, radius=100.0, ccw=True, cx=0.0, cz=0.0):
    """完美圆的坐标点列（等角采样 = 近似等弧长）。"""
    sgn = 1.0 if ccw else -1.0
    return [(cx + math.cos(2 * math.pi * k / samples) * radius,
             cz + math.sin(2 * math.pi * k / samples) * radius * sgn)
            for k in range(samples)]


def _oval_pts(samples=1500, a=200.0, b=80.0):
    """椭圆「跑道」形状（与圆明显不同）。"""
    return [(math.cos(2 * math.pi * k / samples) * a,
             math.sin(2 * math.pi * k / samples) * b)
            for k in range(samples)]


def _lap_from_pts(pts, lap=1, dt=0.033, t0=1000.0):
    """坐标点列 → clean_laps 风格的帧列表（每帧等时，速度无所谓）。"""
    return [{"t": t0 + i * dt, "lap": lap, "car_x": x, "car_z": z}
            for i, (x, z) in enumerate(pts)]


def test_same_shape_different_offset_scale_direction():
    """同形状（圆），平移 + 缩放 + 反向 ⇒ 指纹距离 ≈ 0。

    归一化要抹掉平移/尺度/旋转/绕行方向，剩下的才是形状本身。
    """
    g1 = {1: _lap_from_pts(_circle_pts())}
    g2 = {7: _lap_from_pts(_circle_pts(radius=55.0, ccw=False, cx=9999.0,
                                        cz=-4242.0), lap=7)}
    f1 = track_fingerprint(g1)
    f2 = track_fingerprint(g2)
    assert "error" not in f1 and "error" not in f2
    assert fingerprint_distance(f1["desc"], f2["desc"]) < 0.01


def test_different_shapes_far_apart():
    """圆 vs 椭圆跑道 ⇒ 距离显著大于同形状的 ~0.006 实测量级。"""
    g1 = {1: _lap_from_pts(_circle_pts())}
    g2 = {1: _lap_from_pts(_oval_pts())}
    d = fingerprint_distance(track_fingerprint(g1)["desc"],
                             track_fingerprint(g2)["desc"])
    assert d > 0.1


def test_representative_lap_ignores_dirty_and_partial():
    """脏圈（绕出去一大段）/ 残圈（只有半圈）不得当选代表圈。

    过滤逻辑与 sector_times 同源：圈长相对**中位数** ±5% 先剔，
    剩下的挑用时最短。
    """
    clean = _circle_pts()
    # 脏圈：中段拐出去很远再回来（路径长度显著变大）
    dirty = clean[:]
    for k in range(300, 500):
        x, z = dirty[k]
        dirty[k] = (x + 400.0, z + 400.0)
    # 残圈：只有半圈
    partial = clean[:len(clean) // 2]
    grouped = {
        1: _lap_from_pts(clean, lap=1, t0=1000.0),
        2: _lap_from_pts(dirty, lap=2, t0=2000.0),
        3: _lap_from_pts(partial, lap=3, t0=3000.0),
    }
    fp = track_fingerprint(grouped)
    assert "error" not in fp
    assert fp["lap"] == 1, "代表圈必须是干净完整圈"
    assert fp["n_full"] == 1      # 中位数过滤后只剩干净圈


def test_unsync_frequency_helper_rejects_empty():
    """空分组 / 无效坐标 → error 而非抛错。"""
    out = track_fingerprint({})
    assert "error" in out
    out2 = track_fingerprint({1: [{"t": 1.0, "car_x": None, "car_z": None}]})
    assert "error" in out2


def test_resample_and_normalize_basics():
    """重采样 n 点、归一化后 RMS 半径 = 1、起点在 +X 轴上。"""
    pts = _circle_pts()
    rs = _fp_resample(pts, 200)
    assert rs is not None and len(rs) == 200
    desc, meta = _fp_normalize(rs)
    assert desc is not None and len(desc) == 200
    # RMS 半径归一 ⇒ sqrt(mean(x²+z²)) ≈ 1
    rms = math.sqrt(sum(x * x + z * z for x, z in desc) / len(desc))
    assert rms == pytest.approx(1.0, abs=1e-6)
    # 起点旋到 +X ⇒ 第一个点 y≈0、x≥0
    assert desc[0][1] == pytest.approx(0.0, abs=1e-6)
    assert desc[0][0] >= 0.0
    assert meta["turns_cw"] is False


def test_distance_length_mismatch_is_inf():
    """长度不等 / 空列表 ⇒ inf（绝不给半个假距离）。"""
    a = [[0.0, 0.0]] * 200
    b = [[0.0, 0.0]] * 199
    assert fingerprint_distance(a, b) == float("inf")
    assert fingerprint_distance([], b) == float("inf")