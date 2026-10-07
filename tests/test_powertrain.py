"""动力类型识别 + 电量/能量字段测试。

核心判据（官方字段说明 + 社区 getPowertrainType）：
  fuelCapacity == 0 → 电动；== 5 → 卡丁车；> 0 → 燃油/混动。
纯电时 gas_level 是**剩余电量 kWh**；能量回收等扩展字段只在 ~ 包有值。

⚠️ 包长守门是关键：格式 A 只有 296 字节，绝不能按 ~ 的偏移读 A 包，
   否则会把轮速/胎径误读成能量回收。这里同时断言「A 包返回 0」和「~ 包返回真实值」。
"""
from packets import build_packet


def _decode(rec, dec, **kw):
    return dec.decode(build_packet(True, 40.0, 100.0, 50.0, **kw), 1000.0)


class TestPowertrainClassify:
    def test_fuel_default(self, rec, dec):
        s = _decode(rec, dec)                    # 默认 cap=100
        assert s.powertrain == "fuel"

    def test_electric(self, rec, dec):
        s = _decode(rec, dec, powertrain="electric", gas=28.5)
        assert s.powertrain == "electric"
        assert s.gas_capacity == 0.0             # 纯电容量为 0
        assert abs(s.gas_level - 28.5) < 1e-4    # gas_level 是 kWh

    def test_kart(self, rec, dec):
        s = _decode(rec, dec, powertrain="kart")
        assert s.powertrain == "kart"
        assert abs(s.gas_capacity - 5.0) < 1e-4

    def test_hybrid_is_fuel(self, rec, dec):
        """GT7 不对混动单独标记：混动车容量仍 > 0 → 归为 fuel。"""
        s = _decode(rec, dec, powertrain="fuel", cap=80.0)
        assert s.powertrain == "fuel"


class TestEnergyFields:
    def test_ext_packet_reads_energy(self, rec, dec):
        """~ 扩展包：能量回收/滤波输入读到真实值。"""
        s = _decode(rec, dec, pkt="~", energy_recovery=35.0,
                    throttle_filtered=0.8, brake_filtered=0.2)
        assert abs(s.energy_recovery - 35.0) < 1e-2
        assert abs(s.throttle_filtered - 0.8) < 1e-2
        assert abs(s.brake_filtered - 0.2) < 1e-2

    def test_a_packet_keeps_energy_zero(self, rec, dec):
        """格式 A 只有 296 字节，能量字段必须守门为 0（不能越界/串读）。"""
        s = _decode(rec, dec, pkt="A", energy_recovery=35.0,
                    throttle_filtered=0.8, brake_filtered=0.2)
        assert s.energy_recovery == 0.0
        assert s.throttle_filtered == 0.0
        assert s.brake_filtered == 0.0

    def test_b_packet_no_energy(self, rec, dec):
        """B 包（316B）也没有能量段，仍应为 0。"""
        s = _decode(rec, dec, pkt="B", energy_recovery=35.0)
        assert s.energy_recovery == 0.0

    def test_layout_detected_by_size(self, rec, dec):
        """包类型按实际长度推断：A=296 / B=316 / ~=332。"""
        assert _decode(rec, dec, pkt="A").layout == "A"
        assert _decode(rec, dec, pkt="B").layout == "B"
        assert _decode(rec, dec, pkt="~").layout == "~"