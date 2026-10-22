"""离线分析 C 包抓包：转向角符号 / surfaceType / currentLap 候选偏移。

用法：python tools/analyze_capture_c.py capture_C2.jsonl
不联网、不连 PS5，纯本地解密 + 统计。
"""
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe_fields import salsa20_xor  # noqa: E402

MAGIC = 0x47375330
MASK_A = 0xDEADBEAF
MASK_C = 0xDEADBEEF


def decrypt(raw: bytes):
    if len(raw) < 0x48:
        return None
    iv1 = struct.unpack_from("<I", raw, 0x40)[0]
    for mask in (MASK_A, MASK_C):
        nonce = struct.pack("<II", iv1 ^ mask, iv1)
        p = salsa20_xor(raw, nonce)
        if struct.unpack_from("<I", p, 0)[0] == MAGIC:
            return p
    return None


def f32(b, o):
    return struct.unpack_from("<f", b, o)[0]


def f3(b, o):
    return list(struct.unpack_from("<3f", b, o))


def corr(a, b):
    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    ma = sum(a[:n]) / n
    mb = sum(b[:n]) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a[:n]))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b[:n]))
    if sa < 1e-12 or sb < 1e-12:
        return 0.0
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (sa * sb)


def load(path: str):
    frames = []
    bad = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            raw = bytes.fromhex(d["enc"])
            if len(raw) != 368:
                continue
            p = decrypt(raw)
            if p is None:
                bad += 1
                continue
            frames.append((d["t"], p))
    print(f"解出 {len(frames)} 帧，失败 {bad}")
    return frames


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "capture_C2.jsonl"
    frames = load(path)

    rows = []
    for t, p in frames:
        spd = f32(p, 0x4C)
        pos = f3(p, 0x04)
        vel = f3(p, 0x10)
        ang = f3(p, 0x2C)
        flags = struct.unpack_from("<H", p, 0x8E)[0]
        lap = struct.unpack_from("<H", p, 0x74)[0]
        st_l = f32(p, 0x160)
        st_r = f32(p, 0x164)
        wb = f32(p, 0x168)
        cat = p[0x16C:0x170].split(b"\x00")[0].decode("ascii", "replace")
        # 轮速（rad/s，带符号 → 取绝对值比"走得多快"）与胎温（FL/FR/RL/RR）
        wfl, wfr = abs(f32(p, 0xA4)), abs(f32(p, 0xA8))
        wrl, wrr = abs(f32(p, 0xAC)), abs(f32(p, 0xB0))
        tfl, tfr = f32(p, 0x60), f32(p, 0x64)
        if flags & 0x02 or flags & 0x04:      # 暂停/加载
            continue
        if spd < 5.0:
            continue
        rows.append(dict(t=t, spd=spd, pos=pos, vel=vel, ang=ang,
                         lap=lap, st_l=st_l, st_r=st_r, wb=wb, cat=cat,
                         wr_diff=(wfr + wrr) - (wfl + wrl),
                         t_diff=tfr - tfl))
    print(f"可用帧 {len(rows)}（速度≥5m/s，排除暂停/加载）")
    if not rows:
        return 1

    stl = [r["st_l"] for r in rows]
    strr = [r["st_r"] for r in rows]
    wbs = [r["wb"] for r in rows]
    cats = {r["cat"] for r in rows}
    print(f"steer_l: min={min(stl):.4f} max={max(stl):.4f} |mean|={sum(abs(x) for x in stl)/len(stl):.4f}")
    print(f"steer_r: min={min(strr):.4f} max={max(strr):.4f}")
    print(f"wheel_base: min={min(wbs):.3f} max={max(wbs):.3f}  car_category={cats}")

    # —— 航向角与横摆角速度的手性判定 ——
    # θ = atan2(vz, vx)；右手系（y 向上）时 dθ/dt 与 ω_y 反号，左手系同号。
    thetas, dthetas, omegas, alats = [], [], [], []
    for i in range(1, len(rows)):
        dt = rows[i]["t"] - rows[i - 1]["t"]
        if not (0.001 < dt < 0.5):
            continue
        v0, v1 = rows[i - 1]["vel"], rows[i]["vel"]
        if math.hypot(v0[0], v0[2]) < 3 or math.hypot(v1[0], v1[2]) < 3:
            continue
        th0 = math.atan2(v0[2], v0[0])
        th1 = math.atan2(v1[2], v1[0])
        d = th1 - th0
        while d > math.pi:
            d -= 2 * math.pi
        while d < -math.pi:
            d += 2 * math.pi
        dthetas.append(d / dt)
        omegas.append(rows[i]["ang"][1])
        # 横向加速度：与 recorder 同一套算法（perp = up × u）
        acc = [(v1[k] - v0[k]) / dt for k in range(3)]
        u = [x / math.sqrt(sum(y * y for y in v0)) for x in v0]
        px, pz = u[2], -u[0]
        pm = math.hypot(px, pz)
        alats.append(acc[0] * px / pm + acc[2] * pz / pm if pm > 1e-6 else 0.0)
        stl_ok = True

    # 与 ω_y 对齐后的转向角序列
    stl_mid = [rows[i]["st_l"] for i in range(1, len(rows))][:len(dthetas)]
    print()
    print("=== 手性 ===")
    print(f"corr(dθ/dt, ω_y)          = {corr(dthetas, omegas):+.3f}")
    print(f"corr(steer_l, ω_y)        = {corr(stl_mid, omegas):+.3f}")
    print(f"corr(steer_l, a_lat)      = {corr(stl_mid, alats):+.3f}")
    print(f"corr(a_lat, ω_y)          = {corr(alats, omegas):+.3f}")

    # —— 独立物理验证：转向时外侧轮走更长的弧，转速更快 ——
    # 这条不依赖任何坐标手性假设，只用"轮子转多快"这一个实测物理量：
    #   左转 → 右侧两轮（FR/RR）弧长更长 → 右侧轮速 > 左侧轮速。
    # 所以 corr(a_lat, 右侧轮速−左侧轮速) > 0 就说明 a_lat 为正 = 左转。
    wr, tmp = [], []
    for i in range(1, len(rows)):
        wr.append(rows[i]["wr_diff"])
        tmp.append(rows[i]["t_diff"])
    n = min(len(wr), len(alats))
    print()
    print("=== 独立物理验证（不依赖坐标系假设）===")
    print(f"corr(a_lat, 右轮速−左轮速)   = {corr(alats[:n], wr[:n]):+.3f}")
    print(f"corr(a_lat, 右胎温−左胎温)   = {corr(alats[:n], tmp[:n]):+.3f}")
    print(f"corr(steer_l, 右轮速−左轮速) = {corr(stl_mid[:n], wr[:n]):+.3f}")

    # —— 圈数相关：找 currentLap 偏移 ——
    print()
    print("=== 0x150..0x170 区域（找 surfaceType / currentLap）===")
    def col(off, fmt, size):
        vals = []
        for _, p in frames[:2000]:
            vals.append(struct.unpack_from(fmt, p, off)[0])
        return vals
    for off in range(0x148, 0x170, 2):
        v16 = col(off, "<h", 2)
        u16 = col(off, "<H", 2)
        uniq16 = sorted(set(v16))[:6]
        print(f"  0x{off:03X} i16 uniq={uniq16} min={min(v16)} max={max(v16)}"
              f" | u16 uniq={sorted(set(u16))[:6]}")
    for off in range(0x148, 0x170, 4):
        fl = col(off, "<f", 4)
        print(f"  0x{off:03X} f32 min={min(fl):.4f} max={max(fl):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
