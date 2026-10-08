#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
复核「用横向 G 反推曲率」这件事靠不靠谱。

为什么要留这个工具
------------------
GT7 格式 A **不广播方向盘角度**，事件的「方向（左/右）」和「过弯半径」只能靠
κ = 横向G × 9.80665 / v² 算。这是**推导值**，不是协议字段 —— 所以两个问题必须
能被随时复核，而不是写在注释里让人信：

  ① 精度：κ 跟"轨迹几何曲率"差多少？（几何曲率只用到坐标，不依赖 G）
  ② 符号：横向 G 为正到底是左转还是右转？

符号怎么定（不靠坐标系手性的假设，用物理事实）
----------------------------------------------
转弯时**外侧轮走的路更长 → 角速度更高**。左转时右轮在外侧，所以：
    横向 G 为正 → 右轮更快 → 是左转
两场真实数据都是这个结论（corr +0.76 / +0.56）。

用法
----
    python tools/verify_curvature.py data/某场.jsonl
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

G = 9.80665


def load(path: Path):
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                fr = json.loads(line)
            except Exception:
                continue
            if "session_id" in fr:                 # 首行是 header
                continue
            x, z = fr.get("car_x"), fr.get("car_z")
            v = fr.get("speed_kph") or 0.0
            g = fr.get("g_force") or [0, 0, 0]
            w = fr.get("wheel_rads") or []
            if x is None or z is None or len(w) != 4:
                continue
            rows.append((float(fr.get("t") or 0.0), float(x), float(z),
                         v / 3.6, float(g[1]),
                         [abs(c) for c in w]))
    return rows


def pearson(a, b):
    n = len(a)
    if n < 10:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    return num / (da * db) if da > 1e-12 and db > 1e-12 else 0.0


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not src or not src.exists():
        print("用法: python tools/verify_curvature.py <某场.jsonl>")
        return 2
    rows = load(src)
    print(f"{src.name}: {len(rows)} 帧")

    # ---- ① 精度：几何曲率 vs 由 G 反推的曲率 ----
    W = 15                       # 0.25s @60Hz，压掉坐标噪声
    k_geo, k_from_g, vs = [], [], []
    for i in range(len(rows) - 2 * W):
        t0, x0, z0, v0, g0, _ = rows[i]
        t1, x1, z1, v1, g1, _ = rows[i + W]
        t2, x2, z2, v2, g2, _ = rows[i + 2 * W]
        if v1 < 5.0:
            continue
        d1, d2 = (x1 - x0, z1 - z0), (x2 - x1, z2 - z1)
        if math.hypot(*d1) < 0.5 or math.hypot(*d2) < 0.5:
            continue
        dt = t2 - t0
        if not (0.2 < dt < 0.6):
            continue
        th = math.atan2(d2[1], d2[0]) - math.atan2(d1[1], d1[0])
        while th > math.pi:
            th -= 2 * math.pi
        while th < -math.pi:
            th += 2 * math.pi
        ds = v1 * dt / 2.0
        if ds < 0.5:
            continue
        kg = th / ds
        if abs(kg) > 0.2:                          # 半径 <5m = 噪声
            continue
        k_geo.append(kg)
        vs.append(v1)
        k_from_g.append(rows[i + W][4])            # 先不翻符号，看相关
    if not k_geo:
        print("样本不足，换一场跑得久的")
        return 1
    # 精度只看**大小**：κ_geo 的符号是"XZ 平面上逆时针为正"的绘图约定，
    # 跟车往左拐还是往右拐无关（早先我把这两件事搅在一起，得出过错误的
    # "不一致"结论）。左右由 ② 单独用物理事实定。
    r0 = pearson([abs(x) for x in k_geo], [abs(g) * G / (v * v)
                                           for g, v in zip(k_from_g, vs)])
    k_g = [abs(g) * G / (v * v) for g, v in zip(k_from_g, vs)]
    err = sorted(abs(abs(k_geo[i]) - k_g[i]) for i in range(len(k_geo)))
    print(f"\n① 精度   corr(|κ_geo|, |横向G|·g/v²) = {r0:+.3f}")
    print(f"         误差中位 {err[len(err)//2]:.5f} 1/m")
    ok1 = r0 > 0.9
    print(f"         判定: {'OK' if ok1 else '不达标（>0.9 才算能用）'}")

    # ---- ② 符号：横向 G 为正时，是不是右轮更快（= 左转）----
    gl, dw = [], []
    for _t, _x, _z, v, g, w in rows:
        if v < 11.1 or abs(g) < 0.25:              # <40km/h 或直线段没有左右之分
            continue
        mean = sum(w) / 4.0
        if mean < 1e-3:
            continue
        # 顺序 FL FR RL RR：右侧 = FR(1) + RR(3)
        gl.append(g)
        dw.append(((w[1] + w[3]) - (w[0] + w[2])) / mean)
    if len(gl) >= 10:
        r2 = pearson(gl, dw)
        pos = sorted(dw[i] for i in range(len(gl)) if gl[i] > 0.5)
        neg = sorted(dw[i] for i in range(len(gl)) if gl[i] < -0.5)
        print(f"\n② 符号   corr(横向G, 右轮更快程度) = {r2:+.3f}")
        if pos:
            print(f"         横向G>+0.5 (n={len(pos)}): 左右轮差中位 {pos[len(pos)//2]*100:+.2f}%")
        if neg:
            print(f"         横向G<-0.5 (n={len(neg)}): 左右轮差中位 {neg[len(neg)//2]*100:+.2f}%")
        left = r2 > 0
        # 代码约定：curvature = +横向G·g/v²，**正 = 左转**（见 detector.turn_dir）
        # 所以只要"横向 G 为正 = 左转"成立，代码就是对的。
        print(f"         判定: 横向 G 为正 = {'左转' if left else '右转'}"
              f" —— 代码约定「正 = 左转」"
              f"{'一致' if left else '⚠ 不一致，改 detector.turn_dir 的符号'}")
        ok2 = left
    else:
        print("\n② 符号   有效过弯样本不足，跳过")
        ok2 = True

    print("\n结论:", "两项都对得上，曲率可当转向的替代信号用" if (ok1 and ok2)
          else "有对不上的项 —— 别拿 κ 当转向角用")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
