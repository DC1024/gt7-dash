"""status.json 的「进程存活」语义 vs 「在收包」语义 —— 两件事不能混。

背景（一次真踩坑）：
  Docker HEALTHCHECK 原本查 `/data` 是否可写 —— 那个属性在容器生命周期内
  不变，等于一个永远健康的常量，测不出任何东西。改成查 status.json 的
  mtime 后又冒出个反向问题：`_write_status()` **只在收到遥测包时**才被调用
  （goto gt7-recorder.py 的 `_handle_sample`），PS5 一关机文件 mtime 就冻住，
  容器便在用户不玩游戏期间长期 unhealthy。

解法是主循环每 20 秒 `_touch_status_alive()` 刷新 mtime 但**不改内容**。
这一组测试锁死"不改内容"这一点 —— 因为一旦改成重写，
payload["t"] 会变成当前时刻，仪表盘判 connected 就永远为真（伪造在收包）。
"""
import json
import os
import time


class TestTouchStatusAlive:
    def test_文件不存在时建骨架(self, make_recorder, tmp_path):
        """从没收到过包（PS5 一直没开机）也要有新鲜 mtime，否则容器永远红。"""
        sf = tmp_path / "status.json"
        r = make_recorder(status_file=str(sf))
        assert not sf.exists()
        r._touch_status_alive()
        assert sf.exists(), "应该建出骨架，否则 HEALTHCHECK 的 mtime 检查无从下手"
        payload = json.loads(sf.read_text(encoding="utf-8"))
        # 🔴 骨架里必须**没有 frame**：TelemetryHub.refresh 靠 `if not frame: return`
        #    原样忽略它，才不会把"进程活着"误判成"在收包"。
        assert "frame" not in payload

    def test_已有文件只改_mtime_不改内容(self, make_recorder, tmp_path):
        sf = tmp_path / "status.json"
        original = {"t": 111.0, "frames_total": 7, "frame": {"speed_kph": 88.0}}
        sf.write_text(json.dumps(original), encoding="utf-8")

        # 把 mtime 推到很久以前，模拟"PS5 关机两天"
        old = time.time() - 2 * 86400
        os.utime(sf, (old, old))
        assert time.time() - sf.stat().st_mtime > 86400

        r = make_recorder(status_file=str(sf))
        r._touch_status_alive()

        assert sf.stat().st_mtime > old + 86400, "mtime 应该被刷新"
        after = json.loads(sf.read_text(encoding="utf-8"))
        # 这才是重点：内容一个字节都不能变
        assert after == original, "只该 touch，不该重写内容"
        assert after["t"] == 111.0, "t 必须保持原值，否则等于伪造在收包"

    def test_没有_status_file_参数时什么都不做(self, make_recorder):
        r = make_recorder(status_file=None)
        r._touch_status_alive()   # 不应抛异常


class TestHealthSemanticsSeparated:
    """dashboard 侧：mtime 新鲜 ≠ connected；判 connected 只能看 payload['t']。"""

    def _hub(self, dash, tmp_path):
        return dash.TelemetryHub(tmp_path / "status.json")

    def test_新鲜_mtime_但旧_t_不算_connected(self, dash, tmp_path):
        sf = tmp_path / "status.json"
        # 模拟：recorder 一直在 touch（mtime 新鲜），但 PS5 两小时前就关机了
        payload = {"t": time.time() - 7200, "frame": {"speed_kph": 0.0},
                   "history": [], "session_start": 0.0}
        sf.write_text(json.dumps(payload), encoding="utf-8")

        hub = self._hub(dash, tmp_path)
        hub.refresh()
        assert hub.stats()["connected"] is False, \
            "t 是两小时前的，绝不能因为 mtime 新鲜就报 connected"

    def test_骨架文件不算_connected(self, dash, tmp_path):
        """_touch_status_alive 建的最小骨架不能把仪表盘骗成"采集中"。"""
        sf = tmp_path / "status.json"
        sf.write_text(json.dumps({"t": 0, "alive": True, "frames_total": 0,
                                  "session_start": 0.0}), encoding="utf-8")
        hub = self._hub(dash, tmp_path)
        hub.refresh()
        st = hub.stats()
        assert st["connected"] is False
        assert st["frames"] == 0

    def test_last_packet_t_透出给_api_health(self, dash, tmp_path):
        sf = tmp_path / "status.json"
        now = time.time()
        payload = {"t": now, "frame": {"speed_kph": 100.0}, "history": [],
                   "session_start": 0.0}
        sf.write_text(json.dumps(payload), encoding="utf-8")
        hub = self._hub(dash, tmp_path)
        hub.refresh()
        st = hub.stats()
        assert st["last_packet_t"] == now
        assert st["connected"] is True

    def test_从未收到包时_last_packet_t_为_0(self, dash, tmp_path):
        hub = self._hub(dash, tmp_path)   # 文件根本不存在
        hub.refresh()
        assert hub.stats()["last_packet_t"] == 0.0


class TestSchema2Incremental:
    """状态文件增量协议（schema 2）：全量重建 + 按序号续接 + 暂停判活。"""

    @staticmethod
    def _f(q, spd):
        return {"q": q, "t": q * 0.016, "s": spd, "r": 6000, "g": 4,
                "th": 0.5, "bk": 0.0, "lap": 1, "wt": [80] * 4,
                "wf": [50] * 4, "gf": [0.0, 0.0, 0.0]}

    def _write(self, sf, **kw):
        now = time.time()
        payload = {"schema": 2, "seq": kw.pop("seq", 1), "t": now,
                   "last_rx_t": now, "frame": {"speed_kph": 1.0},
                   "history": [], "session_start": 0.0}
        payload.update(kw)
        sf.write_text(json.dumps(payload), encoding="utf-8")

    def test_full_rebuilds_buffer(self, dash, tmp_path):
        sf = tmp_path / "status.json"
        hub = dash.TelemetryHub(sf)
        self._write(sf, seq=3, full=[self._f(1, 10), self._f(2, 20), self._f(3, 30)],
                    history=[self._f(1, 10), self._f(2, 20), self._f(3, 30)])
        hub.refresh()
        assert [h["speed_kph"] for h in hub.snapshot()["history"]] == [10, 20, 30]

    def test_delta_appends_without_duplicates(self, dash, tmp_path):
        sf = tmp_path / "status.json"
        hub = dash.TelemetryHub(sf)
        self._write(sf, seq=3, full=[self._f(1, 10), self._f(2, 20), self._f(3, 30)],
                    history=[self._f(1, 10), self._f(2, 20), self._f(3, 30)])
        hub.refresh()
        # 下一批增量：带一帧重叠（q=3），只有 4/5 是新的
        self._write(sf, seq=5, history=[self._f(3, 30), self._f(4, 40),
                                        self._f(5, 50)])
        hub.refresh()
        assert [h["speed_kph"] for h in hub.snapshot()["history"]] == [10, 20, 30, 40, 50]
        # 同一份文件再读一次（10Hz 轮询可能重复读到）→ 不能重复追加
        hub.refresh()
        assert len(hub.snapshot()["history"]) == 5

    def test_pause_keeps_connected(self, dash, tmp_path):
        """暂停：数据时刻 t 停在 60 秒前，但 last_rx_t 是刚才 → 仍算连接。"""
        sf = tmp_path / "status.json"
        now = time.time()
        sf.write_text(json.dumps({
            "schema": 2, "seq": 100, "t": now - 60, "last_rx_t": now,
            "frame": {"speed_kph": 0.0}, "history": [], "session_start": 0.0,
        }), encoding="utf-8")
        hub = dash.TelemetryHub(sf)
        hub.refresh()
        assert hub.stats()["connected"] is True
        assert hub.stats()["last_rx_t"] == now

    def test_old_schema_still_replaces(self, dash, tmp_path):
        """老接收器（无 schema 字段）仍按整批覆盖处理，不能因升级而失效。"""
        sf = tmp_path / "status.json"
        now = time.time()
        sf.write_text(json.dumps({
            "t": now, "frame": {"speed_kph": 1.0}, "session_start": 0.0,
            "history": [{"t": 0.1, "s": 11.0, "r": 6000, "g": 4, "th": 0.5,
                         "bk": 0.0, "lap": 1, "wt": [80] * 4,
                         "wf": [50] * 4, "gf": [0, 0, 0]}],
        }), encoding="utf-8")
        hub = dash.TelemetryHub(sf)
        hub.refresh()
        assert [h["speed_kph"] for h in hub.snapshot()["history"]] == [11.0]

    def test_snapshot_since_returns_only_new_frames(self, dash, tmp_path):
        """?since=<seq> 只回新增帧；不带 since 时仍是整窗。"""
        sf = tmp_path / "status.json"
        hub = dash.TelemetryHub(sf)
        self._write(sf, seq=3,
                    full=[self._f(1, 10), self._f(2, 20), self._f(3, 30)],
                    history=[self._f(1, 10), self._f(2, 20), self._f(3, 30)])
        snap = hub.snapshot(since=2)
        assert [h["speed_kph"] for h in snap["history"]] == [30]
        assert snap["seq"] == 3
        assert len(hub.snapshot()["history"]) == 3, "不带 since 应返回整窗"

    def test_background_refresh_fills_buffer(self, dash, tmp_path):
        """后台线程按固定频率刷新：慢速 API 消费者也能拿到完整窗口。

        不调用 snapshot（那会顺带 refresh），直接看 _buffer：
        写入 full 后等一拍应得到 3 帧；再写增量后应续到 6 帧。
        """
        sf = tmp_path / "status.json"
        hub = dash.TelemetryHub(sf)
        hub.start_background_refresh(0.02)

        self._write(sf, seq=3,
                    full=[self._f(1, 10), self._f(2, 20), self._f(3, 30)],
                    history=[self._f(1, 10), self._f(2, 20), self._f(3, 30)])
        time.sleep(0.25)
        assert len(hub._buffer) == 3

        self._write(sf, seq=6, history=[self._f(4, 40), self._f(5, 50),
                                        self._f(6, 60)])
        time.sleep(0.25)
        assert len(hub._buffer) == 6

        hub.start_background_refresh(0.02)   # 重复调用不应再起一个线程
