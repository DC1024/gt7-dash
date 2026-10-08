# -*- coding: utf-8 -*-
"""`/api/v1/sessions/<f>/profile` —— 仪表盘侧的取数与路由接线测试。

这个端点是「赛道工程师」的取数入口，所以两件事都要守：
  1. `session_profile()` 的输出形状与记忆化
  2. 路由**真的接上了** —— 历史上最容易的翻车方式是函数写好了但白名单里
     没加 `"/profile"`，于是被下面那条通用 `/api/v1/sessions/<名>` 吞掉，
     返回一个「场次统计」而且 HTTP 200，看日志完全正常。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synth_lap import synth_lap   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _write_session(path: Path, laps: int = 2, radius: float = 600.0,
                   with_coords: bool = True) -> Path:
    lines = [json.dumps({"session_id": path.stem, "circuit": "test",
                         "car": 1302, "powertrain": "fuel",
                         "has_coords": with_coords}, ensure_ascii=False)]
    for lap in range(1, laps + 1):
        for f in synth_lap(radius_m=radius, lap=lap, coords=with_coords):
            lines.append(json.dumps(f, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def sess(dash, tmp_path):
    return _write_session(tmp_path / "20261009_000000_unknown_deadbeef.jsonl")


class TestSessionProfile:
    def test_explicit_lap(self, dash, sess):
        p = dash.session_profile(sess, lap_no=1, step_m=10.0)
        assert "error" not in p, p
        assert p["meta"]["api_version"] == 1
        assert p["meta"]["file"] == sess.name
        assert p["meta"]["available_laps"] == [1, 2]
        assert p["lap"] == 1
        assert p["geometry_used"] is True
        grid = p["grid_m"]
        assert grid[0] == 0.0
        for key in ("speed_kph", "throttle", "brake", "t_rel_s", "glat",
                    "glon"):
            assert len(p[key]) == len(grid), key
        assert len(p["pt"]["x"]) == len(grid)
        assert p["markers"]["brake_in"], "合成数据里有减速弯，必须检出刹车区"
        assert p["markers"]["apex"], "必须有弯心"

    def test_default_lap_is_best(self, dash, sess):
        """不给 lap 应当落到最快圈（与 /raceline 同一约定）。"""
        p = dash.session_profile(sess, lap_no=None)
        assert "error" not in p, p
        best = (dash.analyze_session(sess).get("best_lap") or {}).get("lap")
        assert p["lap"] == best

    def test_unknown_lap_reports_available(self, dash, sess):
        """圈不存在时要明说，而不是悄悄换成别的圈。"""
        p = dash.session_profile(sess, lap_no=99)
        assert "error" in p
        assert p["available_laps"] == [1, 2]

    def test_memoized(self, dash, sess):
        """同一圈同一参数必须命中记忆化（否则每次请求重算整圈）。"""
        a = dash.session_profile(sess, lap_no=1, step_m=10.0)
        b = dash.session_profile(sess, lap_no=1, step_m=10.0)
        assert a == b
        assert a is b, "应当直接返回记忆化对象"

    def test_different_step_not_confused(self, dash, sess):
        """步长不同必须是不同的缓存条目，不能串味。"""
        a = dash.session_profile(sess, lap_no=1, step_m=5.0)
        b = dash.session_profile(sess, lap_no=1, step_m=20.0)
        assert len(a["grid_m"]) > len(b["grid_m"])
        assert a["step_m"] != b["step_m"]

    def test_no_coords_session(self, dash, tmp_path):
        """无坐标场次：不能崩，要如实标记降级。"""
        s = _write_session(tmp_path / "20261009_000001_unknown_cafebabe.jsonl",
                           with_coords=False)
        p = dash.session_profile(s, lap_no=1)
        assert "error" not in p, p
        assert p["geometry_used"] is False
        assert p["pt"]["x"] == []
        assert p["warnings"]


@pytest.fixture(scope="module")
def src() -> str:
    return (ROOT / "gt7-dashboard.py").read_text(encoding="utf-8")


class TestPartialLastLine:
    """正在录制的场次，最后一行可能是写了一半的。

    这是**灾难性**的失败模式：`json.loads` 抛异常 → 被 `_load_frames` 的
    except 兜住 → 返回空 store → 整场 20 万帧在页面上显示成 0 帧。
    而 `/profile` 恰恰只在正在录制的场次上被调用，所以必须守住。
    """

    def test_truncated_last_line_is_dropped_not_fatal(self, dash, sess):
        raw = sess.read_text(encoding="utf-8")
        sess.write_text(raw[:-40], encoding="utf-8")   # 砍掉最后一行尾巴
        p = dash.session_profile(sess, lap_no=1)
        assert "error" not in p, p
        assert p["frames"] > 1000

    def test_missing_final_newline_keeps_all_frames(self, dash, sess):
        """正常写完但最后一个换行符缺失（边界）只丢那一帧，不丢整场。

        比的是**同一圈**修前修后的帧数 —— 别拿整文件行数去比单圈帧数，
        那会差一个「圈数」的倍数（第一版就写错了）。
        """
        before = dash.session_profile(sess, lap_no=2)["frames"]
        raw = sess.read_text(encoding="utf-8")
        sess.write_text(raw.rstrip("\n"), encoding="utf-8")
        after = dash.session_profile(sess, lap_no=2)
        assert "error" not in after, after
        # 被砍掉的是第 2 圈最后一帧，最多少 1 帧
        assert before - after["frames"] <= 1


class TestRouteWiring:
    """路由接线守卫 —— 见模块 docstring 里说的那种「静默被吞掉」。"""

    def test_whitelist_contains_profile(self, src):
        # 白名单块的关键位是那串 or-linked endswith —— 从 /highlights 那行往下找，
        # 别从 `path.startswith("/api/v1/sessions/")` 找（第一处是 /download）
        anchor = 'path.endswith("/highlights")'
        assert anchor in src
        start = src.index(anchor)
        block = src[start:start + 300]
        assert 'path.endswith("/profile")' in block, \
            "/profile 没进逐场次接口白名单，会被通用场次接口吞掉"

    def test_branch_uses_session_profile(self, src):
        assert 'elif path.endswith("/profile"):' in src
        start = src.index('elif path.endswith("/profile"):')
        assert "session_profile(" in src[start:start + 900]

    def test_session_list_exposes_live_flag(self, src):
        """消费方靠 `live` 挑当前那场；没有它只能靠 modified 猜，猜错是静默错。"""
        assert '"live": bool(live and (now - stat.st_mtime) < 20.0),' in src


class TestInProgressLapExcluded:
    """正在跑的那一圈不能当参考圈。

    这是**实机集成时抓到的真 bug**：Coach 首次接入一场正在录制的比赛时，
    圈号最大的那一圈还在跑 —— 时长更短（在 clean_laps 的速度积分口径下
    反而算"最快"）、坐标折线只覆盖半条赛道。拿它做参考，实时最近点定位
    大面积失配（实测横向误差飙到 300 m ≈ 车在 5 秒里跑过的距离），
    而表现形式是"偶尔算错"而不是报错 —— 这种最难查。
    """

    @staticmethod
    def _write(path: Path, laps: int, partial_last: float = 0.0) -> Path:
        """laps 圈完整数据；partial_last > 0 时再追加一圈的部分帧。"""
        lines = [json.dumps({"session_id": path.stem, "circuit": "test",
                             "car": 1302, "powertrain": "fuel",
                             "has_coords": True}, ensure_ascii=False)]
        for lap in range(1, laps + 1):
            for f in synth_lap(radius_m=600.0, lap=lap):
                lines.append(json.dumps(f, ensure_ascii=False))
        if partial_last > 0:
            frames = synth_lap(radius_m=600.0, lap=laps + 1)
            for f in frames[:int(len(frames) * partial_last)]:
                lines.append(json.dumps(f, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _mark_recording(history_dir: Path, on: bool = True) -> None:
        import time as _t
        (history_dir / "status.json").write_text(json.dumps({
            "t": _t.time() if on else 0.0, "recording": on,
            "frame": {"t": 0.0},
        }), encoding="utf-8")

    def test_partial_lap_would_win_without_the_guard(self, dash, tmp_path):
        """先证明这个坑真的存在：不排除进行中的圈时，它会当选。

        没有这条断言，将来有人删掉排除逻辑，测试也照样绿 —— 那就白测了。
        """
        sess = self._write(tmp_path / "s.jsonl", laps=1, partial_last=0.35)
        d = dash.session_profile(sess, lap_no=None)
        assert "error" not in d, d
        # 半圈被当成"最快圈"，圈长明显短于整圈
        assert d["lap"] == 2, d
        assert d["length_m"] < 2 * 3.14159 * 600 * 0.6

    def test_recording_excludes_in_progress_lap(self, dash, tmp_path):
        sess = self._write(tmp_path / "s.jsonl", laps=2, partial_last=0.35)
        self._mark_recording(tmp_path, True)
        d = dash.session_profile(sess, lap_no=None)
        assert "error" not in d, d
        assert d["meta"]["recording"] is True
        assert d["meta"]["available_laps"] == [1, 2]
        assert d["lap"] == 1, d
        # 整圈：圈长应当接近 2πR
        assert d["length_m"] == pytest.approx(2 * 3.14159 * 600, rel=0.02)

    def test_explicit_in_progress_lap_is_rejected(self, dash, tmp_path):
        sess = self._write(tmp_path / "s.jsonl", laps=2, partial_last=0.35)
        self._mark_recording(tmp_path, True)
        d = dash.session_profile(sess, lap_no=3)
        assert "error" in d
        assert d["why"] == "lap_in_progress"
        assert d["available_laps"] == [1, 2]

    def test_not_recording_keeps_all_laps(self, dash, tmp_path):
        """录制已结束 → 最后一圈也是跑完的，不能一刀切掉。"""
        sess = self._write(tmp_path / "s.jsonl", laps=2)
        self._mark_recording(tmp_path, False)
        d = dash.session_profile(sess, lap_no=2)
        assert "error" not in d, d
        assert d["meta"]["recording"] is False
        assert d["lap"] == 2

    def test_single_lap_still_usable_while_recording(self, dash, tmp_path):
        """只有一圈时不能把唯一的一圈也排掉 —— 那样永远没有参考圈。"""
        sess = self._write(tmp_path / "s.jsonl", laps=0, partial_last=1.0)
        self._mark_recording(tmp_path, True)
        d = dash.session_profile(sess, lap_no=None)
        assert "error" not in d, d
        assert d["meta"]["available_laps"] == [1]
