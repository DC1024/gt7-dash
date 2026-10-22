# -*- coding: utf-8 -*-
"""格式 C（368B，GT7 1.68+）的解码契约。

背景：第 4 批实测 GT7 ver.01.720，10783 个真实 C 包，两个关键结论：

  · **解密 mask 是 0xDEADBEEF，不是 0xDEADBEAF**（A/B/~ 用的那个）。
    就差一个字母，用 A 包的规则解 C 包一个都解不开。
  · 字段偏移与 A 包**大部分一致**（rpm@0x3C / speed@0x4C / lap@0x74 /
    flags@0x8E / position@0x04 / velocity@0x10 全部验证通过），
    但 0x160 之后是 C 包独有的。

这里用**合成包**钉住解码逻辑（真实包的固定向量不入库，数据量大）。
"""
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _c_frame(size: int = 368, **over) -> bytes:
    """造一个格式 C 的明文包（默认值都是合理值）。"""
    b = bytearray(size)
    struct.pack_into("<I", b, 0x00, 0x47375330)      # magic "G7S0"
    struct.pack_into("<f", b, 0x04, 100.0)           # car_x
    struct.pack_into("<f", b, 0x08, 5.0)             # car_y
    struct.pack_into("<f", b, 0x0C, 200.0)           # car_z
    struct.pack_into("<f", b, 0x10, 30.0)            # vx
    struct.pack_into("<f", b, 0x14, 0.0)             # vy
    struct.pack_into("<f", b, 0x18, 0.0)             # vz
    struct.pack_into("<f", b, 0x3C, 6000.0)          # rpm
    struct.pack_into("<f", b, 0x4C, 50.0)            # speed m/s
    struct.pack_into("<I", b, 0x70, 12345)           # pack_id
    struct.pack_into("<H", b, 0x74, 2)               # lap
    struct.pack_into("<H", b, 0x76, 3)               # laps_in_race
    struct.pack_into("<H", b, 0x88, 8800)            # min_alert_rpm
    struct.pack_into("<H", b, 0x8A, 9000)            # max_alert_rpm
    struct.pack_into("<H", b, 0x8E, 0x0009)          # flags: on_track + in_gear
    b[0x90] = 0x13                                   # gear=3, suggested=1
    b[0x91] = 255                                    # throttle
    b[0x92] = 0                                      # brake
    if size >= 0x170:                # 只有 C 包（368B）才有这几个字段
        struct.pack_into("<f", b, 0x160, 0.050)      # steer_angle_l
        struct.pack_into("<f", b, 0x164, 0.032)      # steer_angle_r
        struct.pack_into("<f", b, 0x168, 2.90)       # wheel_base
        b[0x16C:0x170] = b"GR1\x00"                  # car_category
    for k, v in over.items():
        off = int(k[1:], 16) if k.startswith("_") else None
        if off is not None:
            b[off] = v if isinstance(v, int) else b[off]
    return bytes(b)


@pytest.fixture()
def dec(rec):
    return rec.Decoder()


class TestLayout:
    def test_368_字节判定为_C(self, dec):
        """🔴 C 必须排在 ~ 之前：368 >= 0x148(328)，顺序写反会被当成 ~。"""
        s = dec._decode_plain(_c_frame(), 1000.0)
        assert s is not None
        assert s.layout == "C", f"368B 被判成 {s.layout} —— 会被当 ~ 包解析"

    @pytest.mark.parametrize("size,want", [
        (296, "A"), (316, "B"), (332, "~"), (344, "~"), (368, "C"),
    ])
    def test_各包长的布局判定(self, dec, size, want):
        s = dec._decode_plain(_c_frame(size=size), 1000.0)
        assert s is not None, f"{size}B 解不出来"
        assert s.layout == want, f"{size}B 判成 {s.layout}，应为 {want}"


class TestCFields:
    def test_转向角(self, dec):
        s = dec._decode_plain(_c_frame(), 1000.0)
        assert s.steer_angle_l == pytest.approx(0.050, abs=1e-5)
        assert s.steer_angle_r == pytest.approx(0.032, abs=1e-5)

    def test_轴距(self, dec):
        s = dec._decode_plain(_c_frame(), 1000.0)
        assert s.wheel_base == pytest.approx(2.90, abs=1e-4)

    def test_组别字符串(self, dec):
        s = dec._decode_plain(_c_frame(), 1000.0)
        assert s.car_category == "GR1"

    def test_组别全零时为_None(self, dec):
        """全 0 的组别字段要变成 None，不是空字符串。"""
        b = bytearray(_c_frame())
        b[0x16C:0x170] = b"\x00\x00\x00\x00"
        s = dec._decode_plain(bytes(b), 1000.0)
        assert s.car_category is None

    def test_A_包字段在_C_包里偏移一致(self, dec):
        """C 包沿用 A 包的 rpm/speed/lap/gear/flags —— 实测验证过。"""
        s = dec._decode_plain(_c_frame(), 1000.0)
        assert s.rpm == pytest.approx(6000.0, abs=1)
        assert s.speed_kph == pytest.approx(50.0 * 3.6, abs=0.5)
        assert s.lap == 2
        assert s.gear == 3          # 0x13 低 4 位
        assert s.suggested_gear == 1
        assert s.car_on_track is True
        assert s.flags == 0x0009

    def test_gear_是位域不是原始字节(self, dec):
        """0x90 原始值可能是 242，但 gear 只取低 4 位。

        🔴 这个用例防的是"看到 242 就以为偏移错了"的误判 —— 实测 C 包的
        0x90 分布是 17~245，一开始我以为 gear 读错了，其实低 4 位就是 1~6 挡。
        """
        b = bytearray(_c_frame())
        b[0x90] = 0xF2              # 242：低 4 位 = 2，高 4 位 = 15
        s = dec._decode_plain(bytes(b), 1000.0)
        assert s.gear == 2
        assert s.suggested_gear == 15


class TestNoCFieldsOnOtherLayouts:
    def test_A_包没有_C_字段(self, dec):
        """None 表示"该包类型不提供"，不是读到 0 —— 两者含义完全不同。"""
        s = dec._decode_plain(_c_frame(size=296), 1000.0)
        assert s.layout == "A"
        assert s.steer_angle_l is None
        assert s.steer_angle_r is None
        assert s.wheel_base is None
        assert s.car_category is None

    def test_A_包的_json_不含_C_字段(self, dec):
        """60Hz 下每帧多 4 个 null ≈ 每小时多 4MB，而它们永远是 null。"""
        s = dec._decode_plain(_c_frame(size=296), 1000.0)
        d = s.to_json()
        for k in ("steer_angle_l", "steer_angle_r", "wheel_base", "car_category"):
            assert k not in d, f"A 包的 json 里出现了 {k}"

    def test_C_包的_json_含_C_字段(self, dec):
        s = dec._decode_plain(_c_frame(), 1000.0)
        d = s.to_json()
        assert d["steer_angle_l"] == pytest.approx(0.050, abs=1e-4)
        assert d["wheel_base"] == pytest.approx(2.90, abs=1e-3)
        assert d["car_category"] == "GR1"

    def test_转向角为_0_时仍会写出(self, dec):
        """转向角 0（方向盘回正）和"没有转向角"是两件事。"""
        b = bytearray(_c_frame())
        struct.pack_into("<f", b, 0x160, 0.0)
        struct.pack_into("<f", b, 0x164, 0.0)
        d = dec._decode_plain(bytes(b), 1000.0).to_json()
        assert "steer_angle_l" in d
        assert d["steer_angle_l"] == 0.0


class TestDefaultIsC:
    """项目默认包类型必须是 C（2026-10-22 决定）。

    理由：实测 C ⊃ A（基础/轮胎/悬挂/轮速/车型/圈速/引擎全部一致），
    只多不少，代价只有带宽 296→368B。所以默认就该用 C。
    这几条测试防的是"有人把默认值改回 A"——那会让转向角悄悄消失。
    """

    def test_recorder_默认包类型是_C(self):
        import re
        text = (ROOT / "gt7-recorder.py").read_text(encoding="utf-8")
        m = re.search(r'"--packet-type",\s*default="(\w+)"', text)
        assert m, "找不到 --packet-type 的默认值"
        assert m.group(1) == "C", (
            f"默认包类型是 {m.group(1)}，应为 C —— A 包没有转向角/轴距/组别")

    def test_compose_默认包类型是_C(self):
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "${GT7_PACKET_TYPE:-C}" in text, (
            "compose 里默认不是 C（应为 ${GT7_PACKET_TYPE:-C}）")

    def test_仍可切回_A(self):
        """兜底能力不能丢：GT7 < 1.68 或 C 出问题时必须能切回 A。"""
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "GT7_PACKET_TYPE=A" in text, "文档里没写怎么切回 A"
        rec = (ROOT / "gt7-recorder.py").read_text(encoding="utf-8")
        assert '"A", "B", "~", "C"' in rec, "--packet-type 的 choices 少了 A"

    def test_帧存白名单含_C_字段(self, dash):
        """不加白名单 = C 包数据被静默丢弃（存不进去，上层读到 None）。"""
        for k in ("steer_angle_l", "steer_angle_r", "wheel_base",
                  "car_category"):
            assert k in dash._FRAME_COL_KIND, f"{k} 不在帧存白名单里"

    def test_请求与实收不符时有警告(self):
        """请求 C 却收到 A 时必须报警 —— 否则用户以为在录转向角。"""
        text = (ROOT / "gt7-recorder.py").read_text(encoding="utf-8")
        assert "请求的包类型是" in text, (
            "没有包类型不符的警告 —— 切换失败会静默录一场没有转向角的数据")
        assert "_layout_warn_t" in text, "警告没有限流，会刷屏"


class TestSteerAngleApi:
    """`/api/v1/live` 里的转向角：符号约定与 null 语义。

    符号是实测出来的（tools/analyze_capture_c.py，10749 帧真实 C 包）：
      · corr(steer_l, ω_y) = +0.85，corr(dθ/dt, ω_y) = −0.93 → 右手系，
        正横摆角速度 = 左转 → **转角为正 = 左转**
      · 独立物理验证（不依赖坐标系假设）：corr(转角, 右轮速−左轮速) = +0.45，
        左转时右侧走外圈、轮速更快 —— 与上面一致
    """

    def test_正值是左转_负值是右转(self, dash):
        f = dash._steer_deg({"steer_angle_l": 0.10, "steer_angle_r": 0.08})
        assert f is not None and f > 0, "正转角应保持正号（左转）"
        assert f == pytest.approx(0.09 * 180 / 3.141592653589793, abs=0.01)
        assert dash._steer_deg(
            {"steer_angle_l": -0.10, "steer_angle_r": -0.10}) < 0

    def test_直道_0_度不能被当成缺失(self, dash):
        """0.0 是合法值。写成 `a or b` 会把直道判成 null，整行消失。"""
        assert dash._steer_deg(
            {"steer_angle_l": 0.0, "steer_angle_r": 0.0}) == 0.0

    def test_只有一只轮有值时也能用(self, dash):
        assert dash._steer_deg({"steer_angle_l": 0.05}) is not None
        assert dash._steer_deg({"steer_angle_r": 0.05}) is not None

    def test_A_包没有转角时返回_None(self, dash):
        """A/B/~ 包必须给 null，前端据此隐藏整行 —— 不能假装是 0。"""
        assert dash._steer_deg({"speed_kph": 120.0}) is None

    def test_live_接口带三个_C_字段(self, dash):
        snap = {"latest": {"steer_angle_l": 0.12, "steer_angle_r": 0.10,
                           "wheel_base": 2.895, "car_category": "GR1"}}
        car = dash._v1_live(snap)["car"]
        assert car["steer_angle_deg"] == pytest.approx(6.31, abs=0.05)
        assert car["wheel_base_m"] == 2.895
        assert car["car_category"] == "GR1"

    def test_A_包场次的_live_三个字段是_null(self, dash):
        car = dash._v1_live({"latest": {"speed_kph": 100.0}})["car"]
        assert car["steer_angle_deg"] is None
        assert car["wheel_base_m"] is None
        assert car["car_category"] is None

