"""离线破解 `~` 包（344B）的 nonce 规则。

已知：A/B 用 mask 0xDEADBEAF，C 用 0xDEADBEEF，IV 都在密文 0x40。
`~` 包之前用这两套规则都解不开。这里把「IV 偏移 × mask × nonce 顺序 ×
counter 起始」全扫一遍，用 magic 判定，不再靠猜。

用法：python tools/brute_tilde.py capture_reenter.jsonl
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe_fields import _salsa20_core, SALSA_KEY  # noqa: E402

MAGIC = 0x47375330

# 候选 mask：除了已知两个，再加一串"看起来像"的常量
MASKS = [
    0xDEADBEAF, 0xDEADBEEF, 0x00000000,
    0xDEADBEA5, 0xDEADBABE, 0xDEADBEAD, 0xDEADBEE0,
    0xDEADC0DE, 0xDEAD10CC, 0xDEADBEE5, 0xDEADBEE7, 0xDEADFA11,
    0xCAFEBABE, 0xBAADF00D, 0x8BADF00D, 0x0BADF00D,
]


def ks_block(nonce: bytes, counter: int = 0) -> bytes:
    return _salsa20_core(SALSA_KEY, nonce + struct.pack("<Q", counter))


def try_decrypt(raw: bytes):
    """返回 (iv_off, mask, order) 命中列表。"""
    hits = []
    for iv_off in range(0, min(0x60, len(raw) - 8) + 1, 4):
        iv1 = struct.unpack_from("<I", raw, iv_off)[0]
        for mask in MASKS:
            iv2 = iv1 ^ mask
            for order in (0, 1):
                nonce = (struct.pack("<II", iv2, iv1) if order == 0
                         else struct.pack("<II", iv1, iv2))
                ks = ks_block(nonce)
                if struct.unpack_from("<I", bytes(
                        a ^ b for a, b in zip(raw[:4], ks[:4])), 0)[0] == MAGIC:
                    hits.append((iv_off, mask, order))
    return hits


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "capture_reenter.jsonl"
    samples = []
    srcs = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            raw = bytes.fromhex(d["enc"])
            if len(raw) != 344:
                continue
            srcs[d["src"]] = srcs.get(d["src"], 0) + 1
            if len(samples) < 3:
                samples.append(raw)
    print(f"344B 样本 {len(samples)} 个，来源 {srcs}")

    found = set()
    for i, raw in enumerate(samples):
        hits = try_decrypt(raw)
        print(f"  样本 {i}: 命中 {hits or '无'}")
        found.update(hits)

    if not found:
        # 换个角度：magic 可能不在 0x00。直接扫"解密后前 64 字节里有没有 magic"
        print("\n0x00 处没有 magic，改扫解密后前 64 字节任意位置：")
        for iv_off in range(0, 0x60, 4):
            iv1 = struct.unpack_from("<I", samples[0], iv_off)[0]
            for mask in MASKS:
                for order in (0, 1):
                    iv2 = iv1 ^ mask
                    nonce = (struct.pack("<II", iv2, iv1) if order == 0
                             else struct.pack("<II", iv1, iv2))
                    ks = ks_block(nonce)
                    pt = bytes(a ^ b for a, b in zip(samples[0][:64], ks))
                    for off in range(0, 60):
                        if struct.unpack_from("<I", pt, off)[0] == MAGIC:
                            print(f"  命中 iv_off=0x{iv_off:X} mask=0x{mask:08X} "
                                  f"order={order} magic@{off}")
        return 1
    print("\n结论：", found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
