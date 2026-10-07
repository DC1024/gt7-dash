"""扩展解码字段：引擎健康 / 比赛信息 / 换挡提示转速。

⚠️ 这些偏移全部来自 InvoGT 链式偏移表（见 gt7-recorder.py 解码器文档），
   测试用**已知值写入 → 断言读出**，保证偏移没写错。偏移写错在这里必爆。
"""
from packets import build_packet

def _decode(rec, dec, **kw):
    return dec.decode(build_packet(True, 40.0, 100.0, 50.0, **kw), 1000.0)

class TestEngineHealth:
    def test_oil_pressure(self, rec, dec):
        assert abs(_decode(rec, dec, oil_pressure=4.5).oil_pressure - 4.5) < 1e-4

    def test_water_temp(self, rec, dec):
        assert abs(_decode(rec, dec, water_temp=85.0).water_temp - 85.0) < 1e-4

    def test_oil_temp(self, rec, dec):
        assert abs(_decode(rec, dec, oil_temp=95.0).oil_temp - 95.0) < 1e-4

    def test_body_height(self, rec, dec):
        assert abs(_decode(rec, dec, body_height=0.12).body_height - 0.12) < 1e-4


class TestRaceInfo:
    def test_time_of_day(self, rec, dec):
        assert _decode(rec, dec, time_of_day=45296000).time_of_day == 45296000

    def test_quali_pos(self, rec, dec):
        assert _decode(rec, dec, quali_pos=7).quali_pos == 7

    def test_num_cars(self, rec, dec):
        assert _decode(rec, dec, num_cars=20).num_cars == 20


class TestShiftAlert:
    def test_alert_rpm_range(self, rec, dec):
        s = _decode(rec, dec, min_alert_rpm=6200, max_alert_rpm=8800)
        assert s.min_alert_rpm == 6200 and s.max_alert_rpm == 8800

    def test_turbo_still_ok(self, rec, dec):
        """涡轮与新字段同在 0x50 段，确认没互相覆盖。"""
        s = _decode(rec, dec, turbo=1.85, oil_pressure=4.2)
        assert abs(s.turbo_boost - 1.85) < 1e-4
        assert abs(s.oil_pressure - 4.2) < 1e-4
