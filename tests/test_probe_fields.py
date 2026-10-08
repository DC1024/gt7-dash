# -*- coding: utf-8 -*-
"""协议字段探针（tools/probe_fields.py）的回归测试。

盯三件事：
  1. Salsa20 的**密钥流**——用跟 Go 生产解密器对拍过的那串字节钉死。
     （手写 Salsa20 改错一个常量照样能"跑通"，只有固定基线才拦得住）
  2. 加/解密往返——含那个"nonce 取自密文"的循环依赖；
  3. 扫描逻辑——合成包里埋的转向角必须被排到第一名。
"""
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "probe_fields_under_test", ROOT / "tools" / "probe_fields.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pf():
    return _load()


# 2026-10-09 用 gt7-decrypt（Go, golang.org/x/crypto/salsa20）对拍得到的 128 字节
# 密钥流：key = "Simulator Interface Packet GT7 ver 0.0"[:32]，
# nonce = 01 02 03 04 05 06 07 08。改 Salsa20 前先想清楚为什么。
GOLD_KS128 = (
    "fbacf423f7d614fb789c49b168f54fd9ba4eee58ab51c713851f7c1ddc7ca381"
    "d4005ef409d08bc01a5a54f02577406ca8ba07df1dd42dd41732ebf9c4cb74fb"
    "a432d032133986ac7129c72f12412b27356c7e2ebd56dafdb2cee105cba59d62"
    "d9ec2266adaad1a9d6e89fb6b06cfd7333b8cf391e3dd355cb6bf7193bee1ca9"
)


def test_keystream_matches_go_reference(pf):
    ks = pf.salsa20_xor(b"\x00" * 128, bytes([1, 2, 3, 4, 5, 6, 7, 8]))
    assert ks.hex() == GOLD_KS128


def test_key_is_32_bytes_truncated_like_go(pf):
    """Go 写的是 [32]byte(38字节的字面量) → 截断。照抄全 38 字节会全解不开。"""
    assert pf.SALSA_KEY == b"Simulator Interface Packet GT7 ver 0.0"[:32]
    assert len(pf.SALSA_KEY) == 32


def test_encrypt_decrypt_roundtrip(pf):
    """加密 → 解密要拿回原包（含 nonce 取自密文的循环依赖）。"""
    plain = bytearray(296)
    struct.pack_into("<I", plain, 0, pf.MAGIC)
    for i in range(4, 296):
        plain[i] = (i * 37) & 0xFF
    cipher, plain_actual = pf.encrypt_packet_pair(bytes(plain))
    assert pf.decrypt_packet(cipher) == plain_actual
    # 0x40 那 4 个字节是被反推出来的，跟原始入参不同——这是预期，不是 bug
    assert bytes(plain[0x40:0x44]) != plain_actual[0x40:0x44]


def test_decrypt_rejects_garbage(pf):
    assert pf.decrypt_packet(b"\x00" * 296) is None     # magic 对不上
    assert pf.decrypt_packet(b"\x01\x02\x03") is None   # 太短


def test_selftest_finds_planted_steering(pf, capsys):
    """埋在 0x98 的转向角必须被排到第一，否则"没找到"的结论不可信。"""
    rc = pf.selftest()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "0x0098" in out
    assert "SELFTEST OK" in out


def test_analyze_rejects_tiny_samples(pf):
    """样本太少要报错，不能给一个看似正常的排名。"""
    pkts = [(i / 60.0, bytes(296)) for i in range(10)]
    assert "error" in pf.analyze(pkts)
