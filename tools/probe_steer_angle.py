"""用真实记录数据验证：方向盘转角能不能算出来？（离线探针，不连 PS5）

回答两件事：
1. 协议里有没有直接的方向盘转角字段 —— 见 README/结论（没有）；
2. 没有的话，反算出来的前轮转角 δ 可不可信 —— 本脚本用两条**互相独立**
   的路径算曲率 κ，再对拍：
     · κ_G     = 横向G × 9.80665 / v²        （动力学）
     · κ_geo   = 航向变化率 / v              （运动学，只用速度矢量）
   相关高且量级合理 → 说明 δ = atan(L·κ) 可用。

用法（在服务器上，容器内 /data 挂载着历史场次）：
  python3 probe_steer_angle.py /data/xxx.jsonl
"""
import gzip
import json
import math
import sys


def load(path):
    op = gzip.open if path.endswith(".gz") else open
    rows = []
    with op(path, "rt", encoding="utf-8") as fh:
        fh.readline()                       # header
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def usable(f):
    """只要真正在赛道上跑的帧：排除菜单/载入/暂停/静止。"""
    if f.get("loading") or f.get("paused"):
        return False
    if f.get("car_on_track") is False and (f.get("speed_kph") or 0) < 15:
        return False
    lap = f.get("lap") or 0
    if lap <= 0:                            # lap=0 是菜单态
        return False
    v = f.get("speed_kph") or 0.0
    if v < 20.0:                            # 太慢时曲率噪声大
        return False
    vel = f.get("velocity") or []
    if len(vel) < 3:
        return False
    return True


def heading(vel):
    return math.atan2(vel[2], vel[0])


def unwrap(d):
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return d


def pearson(a, b):
    n = len(a)
    if n < 30:
        return float("nan")
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    return num / (da * db) if da > 1e-12 and db > 1e-12 else float("nan")


def pct(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def main(path, L=2.65, RATIO=15.0, win=5):
    rows = load(path)
    fs = [f for f in rows if usable(f)]
    print("总帧 %d，赛道上可用 %d" % (len(rows), len(fs)))
    if len(fs) < 200:
        print("样本太少，换一个真正跑圈的场次")
        return 1

    # 相邻帧位移必须与速度自洽 —— 剔除瞬移（重开/回维修区/跨场次）
    clean = [fs[0]]
    jumps = 0
    for i in range(1, len(fs)):
        a, b = fs[i - 1], fs[i]
        dt = (b.get("t") or 0) - (a.get("t") or 0)
        if not (0.004 < dt < 0.10):
            continue
        v = ((a.get("speed_kph") or 0) + (b.get("speed_kph") or 0)) / 2 / 3.6
        dx = (b.get("car_x") or 0) - (a.get("car_x") or 0)
        dz = (b.get("car_z") or 0) - (a.get("car_z") or 0)
        step = math.hypot(dx, dz)
        if step > max(v * dt * 1.6, 1.0):   # 走得太远 = 瞬移
            jumps += 1
            continue
        clean.append(b)
    fs = clean
    print("剔除瞬移 %d 帧，剩 %d 帧" % (jumps, len(fs)))

    # κ_G：横向 G / v²（GT7 不下发转向角，这是替代路径）
    # κ_geo：航向变化率 / v，窗口拉开到 win 帧降噪
    kg, kgeo, spd = [], [], []
    for i in range(win, len(fs) - win):
        v = (fs[i].get("speed_kph") or 0.0) / 3.6
        g = fs[i].get("g_force") or (0, 0, 0)
        glat = g[1] if len(g) > 1 else 0.0
        dt = (fs[i + win].get("t") or 0) - (fs[i - win].get("t") or 0)
        if not (0.01 < dt < 0.5):
            continue
        dh = unwrap(heading(fs[i + win].get("velocity"))
                    - heading(fs[i - win].get("velocity")))
        if abs(dh) > 0.6:                   # 一个窗口内转超过 34° = 异常
            continue
        kg.append(glat * 9.80665 / (v * v))
        kgeo.append((dh / dt) / v)
        spd.append(v)

    r = pearson(kg, kgeo)
    print("样本 %d 帧；κ(横向G) vs κ(几何) 相关 %+.3f" % (len(kg), r))

    deltas = [math.degrees(math.atan(L * abs(k))) for k in kgeo]
    print("前轮转角 |δ|（L=%.2f m）：中位 %.1f°  p90 %.1f°  p99 %.1f°  最大 %.1f°"
          % (L, pct(deltas, .5), pct(deltas, .9), pct(deltas, .99), max(deltas)))
    print("× 转向比 %.0f:1 → 方向盘：中位 %.0f°  p90 %.0f°  最大 %.0f°"
          % (RATIO, pct(deltas, .5) * RATIO, pct(deltas, .9) * RATIO,
             max(deltas) * RATIO))

    top = sorted(range(len(kgeo)), key=lambda i: -abs(kgeo[i]))[:6]
    print("最急的几处（按几何曲率）：")
    for i in top:
        k = abs(kgeo[i])
        d = math.degrees(math.atan(L * k))
        print("   R=%7.1f m  v=%5.1f m/s(%3.0f km/h)  δ=%4.1f°  方向盘≈%4.0f°"
              % (1 / k, spd[i], spd[i] * 3.6, d, d * RATIO))

    # 按速度分档看正常过弯的转角量级（打滑/甩尾时 κ 会飙到 2G 以上，
    # 那反算出来的是"车实际走的曲率"，不是方向盘位置，单独剔出来看）
    print("\n分速度档（剔掉 |横向G|>1.8G 的打滑帧）：")
    buckets = [(30, 80), (80, 130), (130, 190), (190, 400)]
    for lo, hi in buckets:
        sel = [math.degrees(math.atan(L * abs(kgeo[i])))
               for i in range(len(kgeo))
               if lo <= spd[i] * 3.6 < hi and abs(kg[i]) * spd[i] ** 2 / 9.80665 < 1.8]
        if len(sel) < 50:
            continue
        print("   %3d~%3d km/h：n=%5d  中位 δ %4.1f°  p90 %4.1f°  最大 %4.1f°"
              "  → 方向盘 中位 %3.0f° / p90 %3.0f°"
              % (lo, hi, len(sel), pct(sel, .5), pct(sel, .9), max(sel),
                 pct(sel, .5) * RATIO, pct(sel, .9) * RATIO))
    return 0


if __name__ == "__main__":
    p = sys.argv[1]
    L = float(sys.argv[2]) if len(sys.argv) > 2 else 2.65
    sys.exit(main(p, L=L))
