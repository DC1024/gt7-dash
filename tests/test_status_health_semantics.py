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
