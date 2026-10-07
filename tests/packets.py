"""合成 296 字节 GT7 遥测包 —— 单测共享的包构造器。"""
import struct

MAGIC = 0x47375330

def build_packet(on_track=True, spd_ms=40.0, x=100.0, z=50.0, vx=None, vz=0.0,
                 lap=1, laps_in_race=10, last_lap=0, flags=None,
                 rpm=5000.0, gas=80.0, cap=100.0, gear=3, car_code=1234):
    b = bytearray(296)
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
    b[0x91] = 200
    struct.pack_into("<I", b, 0x124, car_code)
    return bytes(b)
