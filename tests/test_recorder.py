"""录制器回归测试 —— 今天修掉的每个 bug 都对应一个用例。"""
import time

from packets import build_packet


def feed(rec, dec, frames, src="1.2.3.4"):
    """frames: [(packet, t), ...] 走真实 _handle_sample 流程。"""
    for pkt, t in frames:
        smp = dec.decode(pkt, t)
        if smp is not None:
            rec._handle_sample(smp, src, t)


def drive_frames(n, t0, lap_start=1, spd_ms=40.0):
    """在赛道上跑 n 帧（60Hz），圈号每 2 秒 +1，位置前进。"""
    out = []
    for i in range(n):
        lap = lap_start + i // 120
        pkt = build_packet(True, spd_ms + (i % 20) * 0.1, 100 + i * 0.5,
                           50 + i * 0.3, lap=lap, last_lap=95000 + (i // 120) * 5000)  # lastLap 每圈变一次（真实行为）
        out.append((pkt, t0 + i / 60))
    return out


def menu_frames(n, t0):
    """菜单帧（离赛道、静止、带微小抖动）。"""
    return [(build_packet(False, 0.0, 0.001 * i, 0.0, lap=1, last_lap=95000), t0 + i / 60)
            for i in range(n)]


class TestOnTrackJudgement:
    """坑：car_on_track 标志不可靠（车在跑 bit0=0），判定必须取「或」。"""

    def test_driving_with_trackbit_clear_still_records(self, rec, dec, make_recorder, tmp_path):
        """flags=0x0198、118km/h：car_on_track=False 但速度够 → 必须开始录制。"""
        r = make_recorder(off_track_timeout=15.0)
        d = dec
        t = time.time()
        feed(r, d, [(build_packet(True, 33.0, 100, 50, lap=1, flags=0x0198), t + i / 60)
                    for i in range(60)], )
        assert r.session is not None and r.session.recording_started, \
            "速度达标就应开始录制（不能只信 car_on_track 位）"

    def test_menu_frames_never_record(self, rec, dec, make_recorder):
        r = make_recorder()
        t = time.time()
        feed(r, dec, menu_frames(120, t))
        assert (r.session is None) or (not r.session.recording_started)


class TestSessionSplit:
    def test_off_track_timeout_ends_session(self, rec, dec, make_recorder):
        r = make_recorder(off_track_timeout=15.0)
        t = time.time()
        feed(r, dec, drive_frames(300, t))
        feed(r, dec, menu_frames(1200, t + 300 / 60))
        r._check_off_track(t + 300 / 60 + 20)
        assert r.session is None, "离赛道 20 秒（>15）应结束场次"
        assert r.session_count == 1

    def test_short_off_track_does_not_split(self, rec, dec, make_recorder):
        r = make_recorder(off_track_timeout=15.0)
        t = time.time()
        feed(r, dec, drive_frames(300, t))
        n0 = r.session_count
        feed(r, dec, menu_frames(300, t + 300 / 60))       # 5 秒 < 15
        r._check_off_track(t + 600 / 60 + 5)
        assert r.session_count == n0, "短暂离赛道不应切分"

    def test_new_session_starts_on_track_return(self, rec, dec, make_recorder):
        r = make_recorder(off_track_timeout=15.0)
        t = time.time()
        feed(r, dec, drive_frames(300, t))
        feed(r, dec, menu_frames(1200, t + 5))
        r._check_off_track(t + 25)
        assert r.session is None
        t2 = t + 26
        feed(r, dec, drive_frames(120, t2))
        assert r.session_count == 2, "再进赛道应开新场次"


class TestGeoAccumulation:
    """坑：场次结束清空轨迹 / 菜单帧污染轨迹。"""

    def test_path_survives_session_end(self, rec, dec, make_recorder):
        r = make_recorder(off_track_timeout=15.0)
        t = time.time()
        feed(r, dec, drive_frames(300, t))
        p1 = len(r._path)
        assert p1 > 20
        feed(r, dec, menu_frames(1200, t + 5))
        r._check_off_track(t + 25)
        assert len(r._path) == p1, "场次结束后轨迹应保留（跑完的轨迹图不能消失）"

    def test_path_reset_on_new_session(self, rec, dec, make_recorder):
        r = make_recorder(off_track_timeout=15.0)
        t = time.time()
        feed(r, dec, drive_frames(300, t))
        feed(r, dec, menu_frames(1200, t + 5))
        r._check_off_track(t + 25)
        t2 = t + 26
        feed(r, dec, drive_frames(120, t2))
        assert 1 <= len(r._path) <= 30, "新场次轨迹应重新累积"

    def test_menu_frames_do_not_pollute_path(self, rec, dec, make_recorder):
        r = make_recorder(off_track_timeout=15.0)
        t = time.time()
        feed(r, dec, drive_frames(300, t))
        p1 = len(r._path)
        feed(r, dec, menu_frames(600, t + 5))            # 场次还在，但车在菜单
        assert len(r._path) == p1, "菜单帧不得写入轨迹（同坐标垃圾点污染）"


class TestLapTimes:
    def test_lap_time_recorded_once_per_change(self, rec, dec, make_recorder):
        r = make_recorder()
        t = time.time()
        # 恒定 lastLap：没有冲线发生，不应记录（录制启动帧不再误触发）
        feed(r, dec, [(build_packet(True, 40, 100 + i, 50, lap=2, last_lap=95000), t + i / 60)
                      for i in range(120)])
        assert r.lap_times == [], "恒定 lastLap = 没有冲线，不应记录"
        # lastLap 变化 → 冲线，记 1 条
        feed(r, dec, [(build_packet(True, 40, 200 + i, 60, lap=3, last_lap=98000), t + 2 + i / 60)
                      for i in range(120)])
        assert len(r.lap_times) == 1
        assert r.lap_times[0][1] == 98000
        # 再变一次 → 2 条
        feed(r, dec, [(build_packet(True, 40, 300 + i, 70, lap=4, last_lap=99000), t + 4 + i / 60)
                      for i in range(120)])
        assert len(r.lap_times) == 2 and r.lap_times[1][1] == 99000

    def test_lap_times_cleared_on_new_session(self, rec, dec, make_recorder):
        r = make_recorder(off_track_timeout=15.0)
        t = time.time()
        feed(r, dec, drive_frames(300, t))
        assert r.lap_times
        feed(r, dec, menu_frames(1200, t + 5))
        r._check_off_track(t + 25)
        feed(r, dec, drive_frames(120, t + 26))
        assert len(r.lap_times) <= 2, "新场次的圈速应重新攒"
