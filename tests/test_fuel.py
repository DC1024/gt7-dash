"""每圈油耗结算测试：只在冲线时记录，差值 = 圈首 - 圈末。"""
import time

from packets import build_packet


def test_fuel_recorded_per_lap(rec, dec, make_recorder):
    r = make_recorder()
    t = time.time()
    frames = []
    # 3 圈，每圈 2 秒（120 帧），每圈消耗 2%：油量 90 → 88 → 86 → 84
    gas = 90.0
    for lap in range(1, 4):
        for i in range(120):
            g = gas - (i / 120) * 2.0        # 圈内线性消耗 2%
            frames.append((build_packet(True, 40, 100 + i, 50,
                                        lap=lap, last_lap=90000 + lap,
                                        gas=g), t))
            t += 1 / 60
        gas -= 2.0
    from test_recorder import feed
    feed(r, dec, frames)
    # 第1圈起点的标记在录制启动时打；圈1→2、圈2→3 两次冲线 → 2 条
    # （第 3 圈没跑完，不计）
    assert len(r.lap_fuel) == 2, f"应记 2 圈油耗，实际 {r.lap_fuel}"
    for lap_no, cons in r.lap_fuel:
        assert 1.5 <= cons <= 2.5, f"第{lap_no}圈油耗异常: {cons}"
    assert r._fuel_mark == 86.0, "标记 = 最后一次冲线时的油量（第3圈起点 86）"
