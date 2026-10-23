"""从原始抓包里找 `surfaceType`（路面材质）等"小整数数组"字段。

思路：surfaceType 是**每轮一个**的 4 个整数，取值很少（铺装=0，草地/砂石/路肩
各是别的常量）。所以它在整包里的特征非常明显：

  · 值域很小（< 10 个不同值）
  · 大部分时间是 0，偶尔整段非 0
  · **连续 4 个**这样的整数（四个轮子）

用法：
    python tools/analyze_surface.py capture_surface.jsonl

输出：
  1. 所有"值域小且出现过非 0"的偏移（u16 / u32 / f32 三种读法）
  2. 命中候选的时间线（每次跳变的时间点 + 值）
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe_fields import salsa20_xor  # noqa: E402

MAGIC = 0x47375330
MASKS = (0xDEADBEEF, 0xDEADBEAF)


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


def load(path: str, want_len: int = 368):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if d["n"] != want_len:
                continue
            p = decrypt(bytes.fromhex(d["enc"]))
            if p is None:
                continue
            rows.append((d["t"], p))
    return rows


def scan(rows, size: int, fmt: str, label: str):
    """找值域小的整数列。返回 [(off, 不同值个数, 非零比例)]。"""
    cands = []
    n = len(rows)
    if n < 50:
        return cands
    for off in range(0, len(rows[0][1]) - size + 1, 2):
        vals = []
        ok = True
        for _, p in rows:
            try:
                vals.append(struct.unpack_from(fmt, p, off)[0])
            except struct.error:
                ok = False
                break
        if not ok:
            continue
        uniq = set(vals)
        if len(uniq) > 8:
            continue
        nz = sum(1 for v in vals if v != 0) / len(vals)
        if 0.0 < nz < 0.95:          # 偶尔非 0 才是路面材质；恒 0 的是没用的字段
            cands.append((off, len(uniq), round(nz, 3), sorted(uniq)))
    return cands


def timeline(rows, off: int, fmt: str, limit: int = 40):
    out = []
    prev = None
    for t, p in rows:
        v = struct.unpack_from(fmt, p, off)[0]
        if v != prev:
            out.append((round(t, 2), v))
            prev = v
            if len(out) >= limit:
                break
    return out


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "capture_surface.jsonl"
    rows = load(path)
    print(f"解出 {len(rows)} 帧 368B 的 C 包")
    if len(rows) < 100:
        print("帧数太少，先多跑一会儿")
        return 1

    print("\n=== 小整数列候选（u16）===")
    c16 = scan(rows, 2, "<H", "u16")
    for off, nu, nz, uniq in c16:
        print(f"  0x{off:03X}  不同值={nu}  非零比例={nz}  值={uniq}")

    print("\n=== 小整数列候选（u32）===")
    c32 = scan(rows, 4, "<I", "u32")
    for off, nu, nz, uniq in c32:
        print(f"  0x{off:03X}  不同值={nu}  非零比例={nz}  值={uniq}")

    # 打印最有希望的那几个的时间线（帮助对上"哪一段是草地/砂石/路肩"）
    best = sorted(c16 + c32, key=lambda x: -x[2])[:4]
    for off, nu, nz, uniq in best:
        fmt = "<H" if (off, nu, nz, uniq) in c16 else "<I"
        print(f"\n=== 0x{off:03X} ({fmt}) 跳变时间线 ===")
        for t, v in timeline(rows, off, fmt):
            print(f"  t={t:7.2f}  {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
