"""存储归档：老场次就地压缩成 .jsonl.gz（无损）。

60Hz × 52 字段 ≈ 240 MB/小时，一两天就能把盘吃光。归档的办法不是降采样
（丢帧不可逆），而是 gzip —— 所以这里最重要的一条断言是**解压后逐字节相同**：
归档完了读出来的数必须和归档前一模一样，否则这个功能就是在悄悄毁数据。
"""
import gzip
import json
import os
import time

import pytest

HEADER = {"session_id": "abcdef01", "circuit": "test", "car": 1302,
          "powertrain": "fuel", "started_at": 1000.0}


def _write_session(path, laps=2, fps=600, hz=60.0):
    lines = [json.dumps(HEADER, ensure_ascii=False)]
    t = 1000.0
    for lap in range(1, laps + 1):
        for i in range(fps):
            ph = i / fps
            lines.append(json.dumps({
                "t": round(t, 4), "lap": lap,
                "speed_kph": round(180.0 + 20 * ph, 2),
                "rpm": round(5000 + 1000 * ph, 1), "gear": 4,
                "throttle": round(max(0.0, 1 - 2 * ph), 3),
                "brake": round(max(0.0, 2 * ph - 1), 3),
                "g_force": [round(0.5 - ph, 3), round(ph - 0.5, 3), 0.0],
                "gas_level": 60.0, "gas_capacity": 100.0,
                "car_x": 100 + i, "car_z": 50.0,
                "wheel_rads": [100.0, 100.0, 99.0, 99.0],
                "has_coords": True, "layout": "A",
            }, ensure_ascii=False))
            t += 1 / hz
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def sess(tmp_path):
    return _write_session(
        tmp_path / "20261008_000000_unknown_abcdef01.jsonl")


class TestNames:
    @pytest.mark.parametrize("name,want", [
        ("20261008_000000_x_abcdef01.jsonl", "20261008_000000_x_abcdef01"),
        ("20261008_000000_x_abcdef01.jsonl.gz", "20261008_000000_x_abcdef01"),
    ])
    def test_session_stem(self, dash, name, want):
        assert dash.session_stem(name) == want

    def test_is_session_file(self, dash, tmp_path):
        assert dash.is_session_file(tmp_path / "a.jsonl")
        assert dash.is_session_file(tmp_path / "a.jsonl.gz")
        assert not dash.is_session_file(tmp_path / "a.csv")
        assert not dash.is_session_file(tmp_path / "tracks.json")

    def test_resolve_session_补_gz(self, dash, tmp_path):
        """书签里存的是 X.jsonl，磁盘上只有 X.jsonl.gz —— 得兜住。"""
        p = tmp_path / "x.jsonl"
        gz = tmp_path / "x.jsonl.gz"
        gz.write_bytes(b"whatever")
        assert dash.resolve_session(p) == gz
        # 原件还在时优先原件（归档半途被打断的情况）
        p.write_bytes(b"x")
        assert dash.resolve_session(p) == p


class TestArchiveRoundtrip:
    def test_压缩后原件消失且体积变小(self, dash, sess):
        before = sess.stat().st_size
        assert dash.archive_session(sess) is True
        gz = sess.with_name(sess.name + ".gz")
        assert gz.exists() and not sess.exists()
        assert gz.stat().st_size < before

    def test_解压后逐字节相同(self, dash, sess):
        raw = sess.read_bytes()
        dash.archive_session(sess)
        gz = sess.with_name(sess.name + ".gz")
        assert gzip.decompress(gz.read_bytes()) == raw, "归档必须无损"

    def test_归档后读出来的数据一字不差(self, dash, sess):
        """真正要保证的不是「文件一样」，而是「分析结果一样」。"""
        full = dash.session_series(sess, max_points=200)
        dash.archive_session(sess)
        gz = sess.with_name(sess.name + ".gz")
        assert dash.session_series(gz, max_points=200) == full
        # 用老名字（X.jsonl）也能读——书签/收藏里存的都是那个
        assert dash.session_series(
            dash.resolve_session(sess), max_points=200) == full

    def test_最快圈同样能读(self, dash, sess):
        b1 = dash._best_lap_of(sess)
        dash.archive_session(sess)
        assert dash._best_lap_of(sess.with_name(sess.name + ".gz")) == b1

    def test_csv_导出名不带_gz(self, dash, sess):
        """导出的 CSV 该叫 X.csv / X_lap3.csv，不是 X.jsonl.gz.csv。"""
        assert dash.session_stem("X.jsonl.gz") == "X"
        assert dash.session_stem("X.jsonl") == "X"

    def test_已经是_gz_就不再压(self, dash, sess):
        dash.archive_session(sess)
        gz = sess.with_name(sess.name + ".gz")
        assert dash.archive_session(gz) is False


class TestSweep:
    def test_days_le_0_不动(self, dash, sess, tmp_path):
        assert dash.sweep_archive(tmp_path, 0) == 0
        assert sess.exists()

    def test_新场次不动(self, dash, sess, tmp_path):
        """刚落盘的场次不能归档：记录器可能还在往里写。"""
        assert dash.sweep_archive(tmp_path, 30) == 0
        assert sess.exists()

    def test_老场次会被归档(self, dash, sess, tmp_path):
        old = time.time() - 40 * 86400
        os.utime(sess, (old, old))
        assert dash.sweep_archive(tmp_path, 30) == 1
        assert not sess.exists()
        assert (tmp_path / (sess.name + ".gz")).exists()

    def test_正在录制的场次不动(self, dash, sess, tmp_path):
        """live 且近 20s 内还写过 = 记录器正在往里追加，绝不能压。

        压出来是半截，而且归档会删掉原文件——记录器接着就写进一个不存在的
        文件里，这一场直接没了。
        """
        now = time.time()
        os.utime(sess, (now - 5, now - 5))
        assert dash.sweep_archive(tmp_path, 0.0000001, live=True) == 0
        assert sess.exists(), "活场被归档了"
        # 同样的文件、但不在录制中 → 该压就压
        assert dash.sweep_archive(tmp_path, 0.0000001, live=False) == 1
        assert not sess.exists()

    def test_glob_同时看到两种后缀(self, dash, tmp_path):
        _write_session(tmp_path / "a_1_x_aa.jsonl")
        _write_session(tmp_path / "b_2_x_bb.jsonl")
        dash.archive_session(tmp_path / "b_2_x_bb.jsonl")
        names = [f.name for f in dash.glob_sessions(tmp_path)]
        assert names == ["b_2_x_bb.jsonl.gz", "a_1_x_aa.jsonl"]
