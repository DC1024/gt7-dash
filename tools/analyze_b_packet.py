"""分析 B 包（316B = A 的 296 + 5 个 float）里那 5 个额外字段是什么。

B 包只比 A 多 20 字节（0x128..0x13B），社区文档说是
`wheelRotation` / `steeringAngularVelocity` / `sway` / `heave` / `surge`，
但**没人验证过顺序和单位**。这里用真实抓包逐项对拍：

  · sway  ≈ 横向加速度  → 与「速度矢量差分的横向分量」相关
  · surge ≈ 纵向加速度  → 与「纵向分量」相关，且油门为正、刹车为负
  · heave ≈ 垂向加速度  → 与悬挂高度的二阶差分相关，且与纵向/横向都不同相
  · steeringAngularVelocity → 与转角差分相关（B 包没有转角，所以只做量级判断）

用法：python tools/analyze_b_packet.py capture_B2.jsonl
"""
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe_fields import salsa20_xor  # noqa: E402

MAGIC = 0x47375330
MASKS = (0xDEADBEAF, 0xDEADBEEF)
G = 9.80665


def decrypt(raw: bytes):
    if len(raw) < 0x48:
        return None
    iv1 = struct.unpack_from("<I", raw, 0x40)[0]
    for mask in MASKS:
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
    ma, mb = sum(a[:n]) / n, sum(b[:n]) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a[:n]))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b[:n]))
    if sa < 1e-12 or sb < 1e-12:
        return 0.0
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (sa * sb)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "capture_B2.jsonl"
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if d["n"] != 316:
                continue
            p = decrypt(bytes.fromhex(d["enc"]))
            if p is None:
                continue
            rows.append((d["t"], p))
    print(f"解出 {len(rows)} 帧 316B 的 B 包")
    if len(rows) < 100:
        return 1

    # —— 先看这 5 个 float 各自的量级 ——
    print("\n=== 0x128..0x13B 五个 float 的取值范围 ===")
    for off in range(0x128, 0x13C, 4):
        vals = [f32(p, off) for _, p in rows]
        nz = sum(1 for v in vals if abs(v) > 1e-9) / len(vals)
        print(f"  0x{off:03X}: min={min(vals):9.3f} max={max(vals):9.3f} "
              f"|mean|={sum(abs(v) for v in vals)/len(vals):8.3f} 非零={nz:.2f}")

    # —— 参考量：速度矢量差分算的纵/横加速度 + 悬挂变化 ——
    ref = []
    for i in range(1, len(rows)):
        t0, p0 = rows[i - 1]
        t1, p1 = rows[i]
        dt = t1 - t0
        if not (0.001 < dt < 0.2):
            continue
        v0, v1 = f3(p0, 0x10), f3(p1, 0x10)
        spd = f32(p1, 0x4C)
        if spd < 8.0:
            continue
        acc = [(v1[k] - v0[k]) / dt for k in range(3)]
        vm = math.sqrt(sum(x * x for x in v0))
        if vm < 1e-3:
            continue
        u = [x / vm for x in v0]
        a_long = sum(acc[k] * u[k] for k in range(3))
        px, pz = u[2], -u[0]
        pm = math.hypot(px, pz)
        a_lat = (acc[0] * px / pm + acc[2] * pz / pm) if pm > 1e-6 else 0.0
        susp = [f32(p1, o) for o in (0xD0, 0xCC, 0xC8, 0xC4)]
        susp0 = [f32(p0, o) for o in (0xD0, 0xCC, 0xC8, 0xC4)]
        d_susp = sum(susp) / 4 - sum(susp0) / 4
        ref.append((p1, a_long / G, a_lat / G, d_susp / dt / dt, dt))

    print(f"\n参考帧 {len(ref)}（速度 ≥ 8 m/s，dt 合理）")
    print("\n=== 与参考量的相关系数（|r| 越大越像）===")
    print("   字段      vs 纵向G   vs 横向G   vs 悬挂二阶差分")
    for off in range(0x128, 0x13C, 4):
        col = [f32(r[0], off) for r in ref]
        r_lon = corr(col, [r[1] for r in ref])
        r_lat = corr(col, [r[2] for r in ref])
        r_hv = corr(col, [r[3] for r in ref])
        print(f"  0x{off:03X}    {r_lon:+8.3f}  {r_lat:+8.3f}   {r_hv:+8.3f}")

    # —— 量级判断：加速度字段若单位是 m/s²，1g ≈ 9.8；若是 g，则 ≈ 1 ——
    print("\n=== 猜测（按量级 + 相关性）===")
    for off in range(0x128, 0x13C, 4):
        col = [f32(r[0], off) for r in ref]
        mx = max(abs(v) for v in col)
        r_lat = corr(col, [r[2] for r in ref])
        r_lon = corr(col, [r[1] for r in ref])
        if abs(r_lat) > 0.6:
            print(f"  0x{off:03X} ≈ 横向加速度（|r|={abs(r_lat):.2f}，峰值 {mx:.2f}）")
        elif abs(r_lon) > 0.6:
            print(f"  0x{off:03X} ≈ 纵向加速度（|r|={abs(r_lon):.2f}，峰值 {mx:.2f}）")
        else:
            print(f"  0x{off:03X} 与纵/横加速度都不强相关（峰值 {mx:.2f}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
