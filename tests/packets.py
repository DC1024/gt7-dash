"""合成 296 字节 GT7 遥测包 —— 单测共享的包构造器。"""
import struct

MAGIC = 0x47375330

def build_packet(on_track=True, spd_ms=40.0, x=100.0, z=50.0, vx=None, vz=0.0,
                 lap=1, laps_in_race=10, last_lap=0, flags=None,
                 rpm=5000.0, gas=80.0, cap=100.0, gear=3, car_code=1234,
                 oil_pressure=4.5, water_temp=85.0, oil_temp=95.0,
                 body_height=0.12, time_of_day=43200000, quali_pos=5,
                 num_cars=16, min_alert_rpm=6000, max_alert_rpm=8500,
                 turbo=0.0, powertrain=None, energy_recovery=0.0,
                 throttle_filtered=0.0, brake_filtered=0.0, pkt="A",
                 throttle=200 / 255, brake=0.0):
    """合成 GT7 遥测包。

    powertrain: 'fuel'/'electric'/'kart'。None 时不改 cap（默认油车）。
    electric → cap=0（gas 作为 kWh 剩余电量）；kart → cap=5。
    pkt: 'A'=296 字节 / 'B'=316 / '~'=332（扩展，含能量回收等）。
    """
    if powertrain == "electric":
        cap = 0.0
    elif powertrain == "kart":
        cap = 5.0
    size = {"A": 296, "B": 316, "~": 332}[pkt]
    b = bytearray(size)
    struct.pack_into("<I", b, 0, MAGIC)
    struct.pack_into("<3f", b, 0x04, x, 1.0, z)          # position
    if vx is None:
        vx = spd_ms
    struct.pack_into("<3f", b, 0x10, vx, 0.0, vz)        # velocity
    struct.pack_into("<f", b, 0x3C, rpm)
    struct.pack_into("<f", b, 0x44, gas)
    struct.pack_into("<f", b, 0x48, cap)
    struct.pack_into("<f", b, 0x4C, spd_ms)              # m/s
    struct.pack_into("<I", b, 0x70, 1000)                # packID
    struct.pack_into("<H", b, 0x74, lap)
    struct.pack_into("<H", b, 0x76, laps_in_race)
    struct.pack_into("<I", b, 0x7C, last_lap)
    if flags is None:
        flags = (0x01 | 0x08) if on_track else 0x00
    struct.pack_into("<H", b, 0x8E, flags)
    for i in range(4):
        struct.pack_into("<f", b, 0x60 + i * 4, 75.0)
    b[0x90] = gear
    # 0x91 油门 / 0x92 刹车（u8，0~255 → 解码器 /255 成 0~1）。
    # 默认油门沿用历史上的 200，避免改动既有用例的语义。
    b[0x91] = max(0, min(255, int(round(throttle * 255))))
    b[0x92] = max(0, min(255, int(round(brake * 255))))
    struct.pack_into("<f", b, 0x38, body_height)
    struct.pack_into("<f", b, 0x50, turbo)
    struct.pack_into("<f", b, 0x54, oil_pressure)
    struct.pack_into("<f", b, 0x58, water_temp)
    struct.pack_into("<f", b, 0x5C, oil_temp)
    struct.pack_into("<I", b, 0x80, time_of_day)
    struct.pack_into("<H", b, 0x84, quali_pos)
    struct.pack_into("<H", b, 0x86, num_cars)
    struct.pack_into("<H", b, 0x88, min_alert_rpm)
    struct.pack_into("<H", b, 0x8A, max_alert_rpm)
    struct.pack_into("<I", b, 0x124, car_code)
    if pkt == "~":
        # 扩展段（MacManley 布局，见 gt7-recorder.py 解码器注释）：
        # 0x13C throttleFiltered(u8) 0x13D brakeFiltered(u8) 0x13E u8 0x13F u8
        # 0x140 torqueVectors(f32) 0x144 energyRecovery(f32) 0x148 unknown(f32)
        b[0x13C] = int(throttle_filtered * 255)
        b[0x13D] = int(brake_filtered * 255)
        struct.pack_into("<f", b, 0x144, energy_recovery)
    return bytes(b)
