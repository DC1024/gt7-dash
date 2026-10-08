# -*- coding: utf-8 -*-
"""合成赛道生成器 —— 给「圈剖面 / 实时定位」这类功能提供**可验算**的输入。

为什么要合成而不是拿真数据：
    真赛道上「参考圈在 812 m 刹车」这个真值没人知道，只能看曲线像不像；
    合成圆上每个量都能手算：圈长 = 2πR、横向 G = v²/(R·g)、
    刹车点在某段圆弧的起点。断言就能写成等式，而不是"看起来对"。
"""
from __future__ import annotations

import math

G = 9.80665


def synth_lap(radius_m: float = 150.0, base_kph: float = 200.0,
              dip_kph: float = 90.0, dip_start_m: float = 400.0,
              dip_len_m: float = 160.0, hz: float = 60.0,
              lap: int = 1, speed_scale: float = 1.0,
              coords: bool = True) -> list[dict]:
    """一圈匀速绕圆 + 一处减速弯的合成遥测。

    参数
    ----
    radius_m    圆半径（圈长 = 2πR）
    base_kph    直段速度
    dip_kph     减速弯最低速
    dip_start_m 减速弯起点（沿弧长的距离）
    dip_len_m   减速弯长度
    speed_scale 给 speed_kph 乘的系数 —— **只动速度不动坐标**，
                用来人为制造「速度积分 vs 几何弧长」的口径差
    coords      False 时 car_x / car_z 给 None（模拟没坐标的场次）
    """
    length = 2.0 * math.pi * radius_m
    dt = 1.0 / hz

    def v_of_s(s: float) -> float:
        """沿弧长的速度（m/s）。正弦减速再恢复。"""
        s = s % length
        v = base_kph
        if dip_start_m <= s <= dip_start_m + dip_len_m:
            k = (s - dip_start_m) / dip_len_m
            v = base_kph - (base_kph - dip_kph) * math.sin(math.pi * k)
        return v / 3.6

    frames: list[dict] = []
    s = 0.0
    t = 0.0
    eps = 1e-3
    while s < length:
        v = v_of_s(s)
        theta = s / radius_m
        glat = (v * v / radius_m) / G
        # 纵向加速度 = v · dv/ds（中心差分，避免噪声）
        a_long = (v_of_s(s + eps) - v_of_s(s - eps)) / (2 * eps) * v
        glon = a_long / G
        if a_long < -0.3:
            brake, throttle = min(1.0, -glon / 1.2), 0.0
        elif a_long > 0.05:
            brake, throttle = 0.0, min(1.0, glon / 0.6)
        else:
            brake, throttle = 0.0, 0.4
        frames.append({
            "t": round(t, 4),
            "lap": lap,
            "speed_kph": round(v * 3.6 * speed_scale, 3),
            "rpm": 7000.0,
            "gear": 4,
            "throttle": throttle,
            "brake": brake,
            # g_force 顺序 = [纵向, 横向, 0]，与记录器一致
            "g_force": [round(glon, 3), round(glat, 3), 0.0],
            "car_x": (round(radius_m * math.cos(theta), 3) if coords else None),
            "car_z": (round(radius_m * math.sin(theta), 3) if coords else None),
        })
        s += v * dt
        t += dt
    return frames
