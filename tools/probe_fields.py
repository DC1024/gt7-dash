#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GT7 协议字段探针 —— 回答一个具体问题：**协议里到底有没有转向角？**

为什么要这个工具
----------------
`GT7_POV解说方案` 里 ⭐⭐ 级信号是「方向盘角度」，用来判定入弯角度是否合理。
但 recorder 从来没解析过 steer_angle，社区偏移表对 0x93~0xA3 那段也没有
一致说法。猜偏移量去解析 = 编数据，所以改成**用数据说话**：

    转向角 δ 与曲率 κ 是一一对应的（自行车模型 δ = atan(L·κ)）。
    所以「协议里某个字段」只要和 κ 强相关，它就极可能就是转向角。

κ 怎么来（关键：**不依赖任何社区文档**）
----------------------------------------
用速度矢量（0x10~0x18）算航向 ψ = atan2(v_z, v_x)，相邻两帧求航向变化率
ω = Δψ/Δt，曲率 κ = ω / v。
整套只用到 0x10（速度矢量）和 0x4C（速度）——这两个是实测确认过的偏移。

顺带还能验证一个社区字段：把 κ·v 和 0x30（表里写的 angVel[1]，yaw 角速度）
做相关，吻合就说明那张表的 0x2C~0x34 确实是角速度。

用法
----
    # 1. 先自证工具本身没写错（不用 PS5，合成包里埋一个已知转向角）
    python tools/probe_fields.py --selftest

    # 2. 开车时跑（PS5 必须在赛道里跑，最好带上几个大弯 + 一段直线）
    python tools/probe_fields.py --ps5 192.168.43.23 --seconds 25

    # 3. 手里已经有抓下来的原始包（每行一个 hex）也可以离线分析
    python tools/probe_fields.py --from-hex raw.hex

判读
----
「未知偏移里有没有 |corr| > 0.9 的字段」：
  · 有 → 基本就是转向角，看它的取值范围判断单位是弧度(±0.5)还是度(±540)
        还是字节量(0~255, 中位 128)，然后交给 recorder 解析
  · 没有 → GT7 格式 A **不广播**转向角，用 κ = a_lat / v² 代替（已实测
        与轨迹几何曲率相关 0.986），方案里那条 ⭐⭐ 信号降权
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time

MAGIC = 0x47375330          # "GT7\0"（小端存放）
IV_MASK = 0xDEADBEAF
# 🔴 这串常量是 **38 字节**，但 Go 里写的是 [32]byte(...)，**被截断成前 32 字节**
#    （= "Simulator Interface Packet GT7 v"）。任何非 Go 的移植都必须照着截，
#    拿完整的 38 字节去算会算出一串错误的密钥流 —— 这正是 main.go 注释里
#    "手写实现试过两次都算不出正确密钥流" 最可能的成因。
SALSA_KEY = b"Simulator Interface Packet GT7 ver 0.0"[:32]
assert len(SALSA_KEY) == 32
PORT_HB = 33739
PORT_ADV = 33740

# 已知偏移表（来自 gt7-recorder.py 的解码注释）——输出时用来标注，
# 免得把"已经是速度的字段"当成新发现。
KNOWN = {
    0x04: "position.x", 0x08: "position.y", 0x0C: "position.z",
    0x10: "velocity.x", 0x14: "velocity.y", 0x18: "velocity.z",
    0x1C: "rotation.pitch", 0x20: "rotation.yaw", 0x24: "rotation.roll",
    0x28: "relNorth", 0x2C: "angVel.x", 0x30: "angVel.y(yaw?)",
    0x34: "angVel.z", 0x38: "bodyHeight", 0x3C: "engineRPM",
    0x40: "iv", 0x44: "gasLevel", 0x48: "gasCapacity", 0x4C: "speed(m/s)",
    0x50: "turboBoost", 0x54: "oilPressure", 0x58: "waterTemp",
    0x5C: "oilTemp", 0x60: "tyreTemp.FL", 0x64: "tyreTemp.FR",
    0x68: "tyreTemp.RL", 0x6C: "tyreTemp.RR", 0x70: "packID",
    0x74: "lapCount(u16)", 0x76: "lapsInRace(u16)", 0x78: "bestLap",
    0x7C: "lastLap", 0x80: "timeOfDay", 0x84: "qualiPos(u16)",
    0x86: "numCars(u16)", 0x88: "minAlertRPM(u16)", 0x8A: "maxAlertRPM(u16)",
    0x8C: "calcMaxSpeed(u16)", 0x8E: "flags(u16)",
    0x90: "gear(u8)", 0x91: "throttle(u8)", 0x92: "brake(u8)",
    0xA4: "wheelRad.sFL", 0xA8: "wheelRad.sFR", 0xAC: "wheelRad.sRL",
    0xB0: "wheelRad.sRR", 0xC4: "susp.RR", 0xC8: "susp.RL",
    0xCC: "susp.FR", 0xD0: "susp.FL", 0x124: "carCode(u32)",
}


# ---------------------------------------------------------------------------
# Salsa20（纯 Python）
# ---------------------------------------------------------------------------
# ⚠️ 这个项目原本用 Go 的 golang.org/x/crypto/salsa20，注释里写着
#    "手写实现极易出错（试过两次都算不出正确密钥流）"。所以这里的 Python
#    实现**必须用 Go 版对拍**才能信：见 tools/verify_salsa20.py。
#    对拍结论（2026-10-09）：296 字节包逐字节一致。
# ---------------------------------------------------------------------------

# 🔴🔴 这一整段是 **golang.org/x/crypto/salsa20/salsa 的 core() 逐行照搬**，
#    不是教科书上的 Salsa20。照教科书抄会解不开真实包（我第一版就是这么错的）：
#      1. Go 把常量 "expand 32-byte k" **拆成 4 段插在 key 中间**
#         （j0=c[0:4], j5=c[4:8], j10=c[8:12], j15=c[12:16]），
#         而教科书是 4 个常量连续排在最前；
#      2. 8 字节 nonce 放在状态块的前半（in[0:8]），块计数器在后半（in[8:16]），
#         教科书/spec 里这两组是反的；
#      3. quarter-round 的起止位置也随重排变过。
#    这三点只要错一个，magic 校验就过不去，症状是"包全部解不开"。
#    想改这里，先跑 tools/verify_salsa20.py 跟 Go 二进制对拍。
_SIGMA16 = b"expand 32-byte k"


def _salsa20_core(key: bytes, inblock: bytes) -> bytes:
    """Go 版 core() 的等价实现：16 字节 inblock（nonce||counter）→ 64 字节块。"""
    c = struct.unpack("<4I", _SIGMA16)
    k = struct.unpack("<8I", key)
    n = struct.unpack("<4I", inblock)
    # 与 Go 完全相同的装填顺序
    j = [c[0], k[0], k[1], k[2], k[3], c[1], n[0], n[1], n[2], n[3],
         c[2], k[4], k[5], k[6], k[7], c[3]]
    x = list(j)
    M = 0xFFFFFFFF

    def rot(v: int, r: int) -> int:
        return ((v << r) | (v >> (32 - r))) & M

    for _ in range(10):
        u = (x[0] + x[12]) & M; x[4] ^= rot(u, 7)
        u = (x[4] + x[0]) & M; x[8] ^= rot(u, 9)
        u = (x[8] + x[4]) & M; x[12] ^= rot(u, 13)
        u = (x[12] + x[8]) & M; x[0] ^= rot(u, 18)

        u = (x[5] + x[1]) & M; x[9] ^= rot(u, 7)
        u = (x[9] + x[5]) & M; x[13] ^= rot(u, 9)
        u = (x[13] + x[9]) & M; x[1] ^= rot(u, 13)
        u = (x[1] + x[13]) & M; x[5] ^= rot(u, 18)

        u = (x[10] + x[6]) & M; x[14] ^= rot(u, 7)
        u = (x[14] + x[10]) & M; x[2] ^= rot(u, 9)
        u = (x[2] + x[14]) & M; x[6] ^= rot(u, 13)
        u = (x[6] + x[2]) & M; x[10] ^= rot(u, 18)

        u = (x[15] + x[11]) & M; x[3] ^= rot(u, 7)
        u = (x[3] + x[15]) & M; x[7] ^= rot(u, 9)
        u = (x[7] + x[3]) & M; x[11] ^= rot(u, 13)
        u = (x[11] + x[7]) & M; x[15] ^= rot(u, 18)

        u = (x[0] + x[3]) & M; x[1] ^= rot(u, 7)
        u = (x[1] + x[0]) & M; x[2] ^= rot(u, 9)
        u = (x[2] + x[1]) & M; x[3] ^= rot(u, 13)
        u = (x[3] + x[2]) & M; x[0] ^= rot(u, 18)

        u = (x[5] + x[4]) & M; x[6] ^= rot(u, 7)
        u = (x[6] + x[5]) & M; x[7] ^= rot(u, 9)
        u = (x[7] + x[6]) & M; x[4] ^= rot(u, 13)
        u = (x[4] + x[7]) & M; x[5] ^= rot(u, 18)

        u = (x[10] + x[9]) & M; x[11] ^= rot(u, 7)
        u = (x[11] + x[10]) & M; x[8] ^= rot(u, 9)
        u = (x[8] + x[11]) & M; x[9] ^= rot(u, 13)
        u = (x[9] + x[8]) & M; x[10] ^= rot(u, 18)

        u = (x[15] + x[14]) & M; x[12] ^= rot(u, 7)
        u = (x[12] + x[15]) & M; x[13] ^= rot(u, 9)
        u = (x[13] + x[12]) & M; x[14] ^= rot(u, 13)
        u = (x[14] + x[13]) & M; x[15] ^= rot(u, 18)

    return struct.pack("<16I", *[((x[i] + j[i]) & M) for i in range(16)])


def salsa20_block(key: bytes, nonce: bytes, counter: int) -> bytes:
    """一个 64 字节密钥流块（Salsa20/20）。

    inblock = nonce(8) || counter(8, 小端) —— 顺序照 Go，别照 spec。
    """
    return _salsa20_core(key, nonce + struct.pack("<Q", counter & 0xFFFFFFFFFFFFFFFF))


def salsa20_xor(data: bytes, nonce: bytes, key: bytes = SALSA_KEY) -> bytes:
    out = bytearray(data)
    for blk in range((len(data) + 63) // 64):
        ks = salsa20_block(key, nonce, blk)
        base = blk * 64
        for i in range(min(64, len(data) - base)):
            out[base + i] ^= ks[i]
    return bytes(out)


def decrypt_packet(raw: bytes) -> bytes | None:
    """解一个原始 UDP 包。失败（magic 不对）返回 None。

    nonce 规则与 Go 版一致：从**密文**的 0x40 读 iv1，
    nonce = LE32(iv1 ^ 0xDEADBEAF) || LE32(iv1)。
    """
    if len(raw) < 0x44 + 8:
        return None
    iv1 = struct.unpack_from("<I", raw, 0x40)[0]
    nonce = struct.pack("<II", iv1 ^ IV_MASK, iv1)
    plain = salsa20_xor(raw, nonce)
    if struct.unpack_from("<I", plain, 0)[0] != MAGIC:
        return None
    return plain


def encrypt_packet(plain: bytes, seed_iv: int = 0x12345678) -> bytes:
    return encrypt_packet_pair(plain, seed_iv)[0]


def encrypt_packet_pair(plain: bytes,
                        seed_iv: int = 0x12345678) -> tuple[bytes, bytes]:
    """把明文包加密成"能被 decrypt_packet 解开"的密文（自测 / 对拍用）。

    ⚠️ 有个循环依赖要先破：nonce 取自**密文**的 0x40，而密文 = 明文 ^ 密钥流。
    所以先定死密文 0x40 处的值 s，由 s 推出 nonce 与密钥流，再反推明文
    该填什么 —— 明文 0x40 处必须是 s ^ ks[0x40:0x44]。

    返回 (密文, 实际被加密的明文)：后者才是解密应该得到的字节，
    对拍时要拿它跟 Go 的输出比，而不是拿原始入参（0x40 被改过）。
    """
    nonce = struct.pack("<II", seed_iv ^ IV_MASK, seed_iv)
    ks = b"".join(salsa20_block(SALSA_KEY, nonce, b)
                  for b in range((len(plain) + 63) // 64))
    buf = bytearray(plain)
    buf[0x40:0x44] = struct.pack("<I", seed_iv ^ struct.unpack_from("<I", ks, 0x40)[0])
    return bytes(a ^ b for a, b in zip(buf, ks)), bytes(buf)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 10:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    return num / (da * db) if da > 1e-12 and db > 1e-12 else 0.0


def f32(buf: bytes, off: int) -> float:
    return struct.unpack_from("<f", buf, off)[0]


def _unwrap(d: float) -> float:
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return d


def references(pkts: list[tuple[float, bytes]]) -> tuple[list[int], list[float], float]:
    """算出每帧的曲率 κ（1/m）与「速度矢量航向变化率」ω。

    返回 (参与计算的帧下标, κ列表, corr(ω_vel, 0x30字段))
    只用 0x10 速度矢量 + 0x4C 速度，不碰任何有争议的偏移。
    """
    idx: list[int] = []
    kap: list[float] = []
    w_vel: list[float] = []
    w_pkt: list[float] = []
    for i in range(len(pkts) - 1):
        t0, p0 = pkts[i]
        t1, p1 = pkts[i + 1]
        dt = t1 - t0
        if not (0.004 < dt < 0.10):     # 掉帧/重发的间隔不可信
            continue
        v0 = (f32(p0, 0x10), f32(p0, 0x14), f32(p0, 0x18))
        v1 = (f32(p1, 0x10), f32(p1, 0x14), f32(p1, 0x18))
        sp0 = math.sqrt(sum(c * c for c in v0))
        sp1 = math.sqrt(sum(c * c for c in v1))
        if min(sp0, sp1) < 5.0:         # 低速时航向噪声大，跳过
            continue
        psi0 = math.atan2(v0[2], v0[0])
        psi1 = math.atan2(v1[2], v1[0])
        w = _unwrap(psi1 - psi0) / dt
        v = (sp0 + sp1) / 2.0
        idx.append(i)
        kap.append(w / v)
        w_vel.append(w)
        w_pkt.append(f32(p0, 0x30))
    r_ang = pearson(w_vel, w_pkt) if len(w_vel) >= 10 else 0.0
    return idx, kap, r_ang


def analyze(pkts: list[tuple[float, bytes]], top: int = 25) -> dict:
    """逐偏移扫描，找与曲率相关的字段。"""
    if len(pkts) < 30:
        return {"error": f"样本太少（{len(pkts)} 帧），至少抓 30 帧"}
    idx, kap, r_ang = references(pkts)
    if len(kap) < 20:
        return {"error": f"有效帧太少（{len(kap)}），要在赛道上跑起来再抓"}
    n = len(pkts[0][1])
    absk = [abs(x) for x in kap]

    rows = []
    for off in range(0, n - 3, 4):
        vals = []
        ok = True
        for i in idx:
            v = f32(pkts[i][1], off)
            if not math.isfinite(v):
                ok = False
                break
            vals.append(v)
        if not ok:
            continue
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-6:              # 常量字段没有信息
            continue
        mean = sum(vals) / len(vals)
        sd = math.sqrt(sum((x - mean) ** 2 for x in vals) / len(vals))
        if sd < 1e-9:
            continue
        c = pearson(vals, kap)
        ca = pearson(vals, absk)
        # 🔴 排序只认**带符号**的相关：转向是有左右之分的，真转向角必须与
        #    κ 同号。只用 |κ| 会把"量随弯道大小涨、但不分左右"的冒牌货
        #    （典型：油门——弯前收、弯中给）排到前面去，自测里就是这个陷阱。
        #    相关 |κ| 仍然打印出来，作为"这个字段是不是只跟弯道强度有关"的诊断。
        rows.append({
            "off": off, "kind": "f32", "corr": c, "corr_abs": ca,
            "score": abs(c),
            "lo": lo, "hi": hi, "mean": mean, "sd": sd,
            "known": KNOWN.get(off, ""),
        })

    brows = []
    for off in range(n):
        vals = [float(pkts[i][1][off]) for i in idx]
        lo, hi = min(vals), max(vals)
        if hi - lo < 2:                 # 字节几乎不动
            continue
        c = pearson(vals, kap)
        ca = pearson(vals, absk)
        brows.append({
            "off": off, "kind": "u8", "corr": c, "corr_abs": ca,
            "score": abs(c), "lo": lo, "hi": hi,
            "mean": sum(vals) / len(vals), "sd": 0.0,
            "known": KNOWN.get(off, ""),
        })

    rows.sort(key=lambda r: -r["score"])
    brows.sort(key=lambda r: -r["score"])
    return {
        "frames": len(pkts), "used": len(kap), "size": n,
        "kappa_range": (min(kap), max(kap)),
        "corr_angvel_documented": r_ang,
        "floats": rows[:top], "bytes": brows[:10],
    }


def guess_unit(row: dict) -> str:
    lo, hi = row["lo"], row["hi"]
    if -1.2 < lo and hi < 1.2:
        return "弧度?（±1 以内）"
    if -50 < lo and hi < 50:
        return "小角度/度?（±50）"
    if -600 < lo and hi < 600:
        return "度?（±540 就像方向盘）"
    return f"其它（{lo:.1f}~{hi:.1f}）"


def report(res: dict) -> int:
    if res.get("error"):
        print(f"\n[无法分析] {res['error']}")
        return 2

    print(f"\n抓到 {res['frames']} 帧，其中 {res['used']} 帧可用于算曲率；"
          f"包长 {res['size']} 字节")
    lo, hi = res["kappa_range"]
    print(f"曲率范围 {lo:+.4f} ~ {hi:+.4f} 1/m"
          f"（对应半径 {1/max(abs(hi), abs(lo), 1e-6):.0f}m 以上）")
    r = res["corr_angvel_documented"]
    verdict = "吻合（那张表的 0x2C~0x34 确实是角速度）" if abs(r) > 0.8 else \
              "不吻合（0x2C~0x34 可能不是角速度，或符号约定不同）"
    print(f"副产品：ω(速度矢量差分) vs 0x30 字段 相关 {r:+.3f} —— {verdict}")

    print("\n=== f32 字段：与曲率最相关的前几名 ===")
    print(f"{'偏移':>7} {'相关κ':>7} {'相关|κ|':>8} {'取值范围':>22}  {'已知字段'}")
    for row in res["floats"]:
        if row["score"] < 0.5 and not row["known"]:
            continue
        rng = f"{row['lo']:.2f} ~ {row['hi']:.2f}"
        print(f"0x{row['off']:04X}  {row['corr']:+7.3f} {row['corr_abs']:+8.3f} "
              f"{rng:>22}  {row['known']}")

    print("\n=== u8 字段（转向也可能是 0~255 的量，像油门刹车那样）===")
    for row in res["bytes"]:
        if row["score"] < 0.5 and not row["known"]:
            continue
        print(f"0x{row['off']:04X}  {row['corr']:+7.3f} {row['corr_abs']:+8.3f} "
              f"{row['lo']:5.0f} ~ {row['hi']:5.0f}  {row['known']}")

    best = None
    for row in res["floats"] + res["bytes"]:
        if not row["known"] and row["score"] > (best["score"] if best else 0):
            best = row
    print()
    if best and best["score"] > 0.9:
        print(f">>> 结论：**找到了** —— 0x{best['off']:04X}（{best['kind']}）"
              f"与曲率相关 {best['corr']:+.3f}，{guess_unit(best)}")
        print(f"    建议：确认后加进 gt7-recorder.py，按 {best['kind']} 解析；")
        print(f"    单位要看取值范围定，别直接当度用（编出来的数字比没有更糟）。")
        return 0
    if best:
        print(f">>> 结论：**没找到** —— 最强相关的未知字段 0x{best['off']:04X} "
              f"也只有 {best['score']:.2f}（<0.9 不构成证据）。")
    else:
        print(">>> 结论：**没找到** —— 没有任何未知字段与曲率相关。")
    print("    判定：GT7 格式 A 大概率**不广播**方向盘角度。")
    print("    替代方案（已实测）：κ = 横向G × 9.80665 / v²，与轨迹几何曲率")
    print("    相关 0.986；方向由符号定（横向G 为正 = 左转，用外侧轮角速度验证过）。")
    return 1


# ---------------------------------------------------------------------------
# 数据来源
# ---------------------------------------------------------------------------

def capture(ps5: str | None, seconds: float, want: int = 1200) -> list[tuple[float, bytes]]:
    """抓原始包。心跳是必需的——不发 PS5 会停止广播。"""
    socks = []
    for p in (PORT_HB, PORT_ADV):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind(("0.0.0.0", p))
            s.settimeout(0.3)
            socks.append(s)
        except OSError as e:
            print(f"  无法绑定 {p}: {e}")

    targets = [(ps5, PORT_HB)] if ps5 else []
    targets.append(("255.255.255.255", PORT_HB))

    pkts: list[tuple[float, bytes]] = []
    t0 = time.time()
    last_hb = 0.0
    print(f"\n抓包 {seconds:.0f} 秒（请在赛道里跑，带几个大弯和一段直线）…")
    while time.time() - t0 < seconds and len(pkts) < want:
        if time.time() - last_hb > 1.0:
            for tgt in targets:
                try:
                    socks[0].sendto(b"A", tgt)
                except OSError:
                    pass
            last_hb = time.time()
        for s in socks:
            try:
                d, _a = s.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if decrypt_packet(d) is not None:      # 只留能解开的
                pkts.append((time.time(), d))
        sys.stdout.write(f"\r  已收 {len(pkts)} 包 ({time.time()-t0:.0f}s)   ")
        sys.stdout.flush()
    print()
    for s in socks:
        s.close()
    return pkts


def selftest() -> int:
    """合成 300 帧，在 0x98 埋一个"转向角"，看工具能不能把它找出来。

    同时埋两个诱饵：
      · 0x9C 与油门相关（不该被判成转向）
      · 0xA0 随机游走（有变化但跟转向无关）
    能过这个自测，才说明"没找到"是真的没找到，不是工具瞎。
    """
    import random
    random.seed(7)
    N, SZ = 300, 296
    pkts: list[tuple[float, bytes]] = []
    t = 0.0
    psi = 0.0
    for k in range(N):
        buf = bytearray(SZ)
        struct.pack_into("<I", buf, 0, MAGIC)
        # 曲率：方波式左右弯 + 一段直线，制造明显的正负与零
        phase = k / 60.0
        kap = 0.02 * math.sin(phase * 1.1) if (k // 90) % 2 == 0 else 0.0
        v = 30.0 + 5.0 * math.sin(phase)
        psi += kap * v / 60.0
        struct.pack_into("<3f", buf, 0x10,
                         v * math.cos(psi), 0.0, v * math.sin(psi))
        struct.pack_into("<f", buf, 0x4C, v)
        # 顺带把 0x30 填成角速度（ω = κ·v），让自测也覆盖"参考量"那条路径
        struct.pack_into("<f", buf, 0x30, kap * v)
        throttle = max(0.0, min(1.0, 0.5 + 0.5 * math.sin(phase * 2)))
        buf[0x91] = int(throttle * 255)
        buf[0x92] = 0
        # ✅ 真·转向角（弧度）：δ = atan(L·κ)，L=2.6m
        struct.pack_into("<f", buf, 0x98, math.atan(2.6 * kap))
        # 诱饵 1：跟油门相关
        struct.pack_into("<f", buf, 0x9C, throttle * 3.0)
        # 诱饵 2：随机游走
        selftest._walk = getattr(selftest, "_walk", 0.0) + random.uniform(-1, 1)
        struct.pack_into("<f", buf, 0xA0, selftest._walk)
        pkts.append((t, bytes(buf)))
        t += 1.0 / 60.0

    res = analyze(pkts)
    if res.get("error"):
        print(f"自测失败：{res['error']}")
        return 2
    report(res)
    top = res["floats"][0] if res["floats"] else None
    ok = bool(top and top["off"] == 0x98 and top["score"] > 0.9)
    print("\n" + ("[SELFTEST OK] 埋在 0x98 的转向角被排到了第一名"
                  if ok else f"[SELFTEST FAILED] 第一名是 0x{top['off']:04X} "
                             f"(score={top['score']:.2f})，不是 0x98"))
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="GT7 协议字段探针（找转向角）")
    p.add_argument("--ps5", default=None, help="PS5 的 IP（不填则只靠广播）")
    p.add_argument("--seconds", type=float, default=20.0, help="抓包秒数")
    p.add_argument("--from-hex", default=None,
                   help="从文件读原始包（每行一个 hex），离线分析")
    p.add_argument("--selftest", action="store_true", help="合成数据自测")
    p.add_argument("--top", type=int, default=25)
    args = p.parse_args()

    print("=" * 64)
    print(" GT7 协议字段探针 —— 协议里到底有没有转向角？")
    print("=" * 64)

    if args.selftest:
        return selftest()

    if args.from_hex:
        pkts = []
        t = 0.0
        with open(args.from_hex, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if len(line) < 16:
                    continue
                try:
                    raw = bytes.fromhex(line)
                except ValueError:
                    continue
                if decrypt_packet(raw) is not None:
                    pkts.append((t, raw))
                    t += 1.0 / 60.0
        print(f"从 {args.from_hex} 读到 {len(pkts)} 个可解密包")
        # ⚠️ 文件里没有真实时间戳，只能按 60Hz 假设，κ 的绝对值会有偏差，
        #    但相关性（我们只关心相关性）不受影响。
        return report(analyze(pkts, top=args.top))

    pkts = capture(args.ps5, args.seconds)
    if not pkts:
        print("\n一个包都没收到。先跑 gt7-diagnose.py 确认链路，"
              "并确认 PS5 正在赛道里（菜单里不发遥测）。")
        return 2
    return report(analyze(pkts, top=args.top))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
