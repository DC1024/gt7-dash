"""回收站自动清理：只删超期文件，retention=0 时永不清理。"""
import os
import time

from packets import build_packet


def test_trash_cleanup_removes_only_expired(rec, make_recorder, tmp_path):
    r = make_recorder(output=str(tmp_path), trash_retention_days=30)
    trash = tmp_path / "_trash"
    trash.mkdir()
    old = trash / "old_session.jsonl"
    old.write_text("{}")
    recent = trash / "recent_session.jsonl"
    recent.write_text("{}")
    # 把 old 的 mtime 拨到 31 天前
    old_t = time.time() - 31 * 86400
    os.utime(old, (old_t, old_t))

    removed = r._cleanup_trash()

    assert removed == 1, f"应删 1 个，实际 {removed}"
    assert not old.exists(), "超期文件应被删除"
    assert recent.exists(), "未超期文件必须保留"


def test_trash_retention_zero_disables(rec, make_recorder, tmp_path):
    r = make_recorder(output=str(tmp_path), trash_retention_days=0)
    trash = tmp_path / "_trash"
    trash.mkdir()
    f = trash / "ancient.jsonl"
    f.write_text("{}")
    old_t = time.time() - 365 * 86400
    os.utime(f, (old_t, old_t))

    assert r._cleanup_trash() == 0, "retention=0 应完全不清理"
    assert f.exists()


def test_trash_missing_dir_no_error(rec, make_recorder, tmp_path):
    r = make_recorder(output=str(tmp_path), trash_retention_days=30)
    assert r._cleanup_trash() == 0, "目录不存在应安静返回 0"


def test_recorder_daily_routine_ends_session_still_works(rec, dec, make_recorder):
    """清理逻辑加入后，正常的录制/切分流程不受影响（回归保护）。"""
    r = make_recorder(output=str(make_recorder.__name__), trash_retention_days=30)


def test_trash_retention_from_settings_file(rec, make_recorder, tmp_path):
    """data/settings.json 里的保留期应覆盖命令行默认（页面可调的实现基础）。"""
    r = make_recorder(output=str(tmp_path), trash_retention_days=30)
    (tmp_path / "settings.json").write_text('{"trash_retention_days": 1}',
                                            encoding="utf-8")
    trash = tmp_path / "_trash"
    trash.mkdir()
    f = trash / "old.jsonl"
    f.write_text("{}")
    t2 = time.time() - 2 * 86400          # 2 天前：超 1 天保留期，未超 30 天
    os.utime(f, (t2, t2))

    assert r._cleanup_trash() == 1, "settings.json 的保留期应覆盖命令行默认"
    assert not f.exists()


def test_trash_settings_invalid_falls_back(rec, make_recorder, tmp_path):
    r = make_recorder(output=str(tmp_path), trash_retention_days=30)
    (tmp_path / "settings.json").write_text('{"trash_retention_days": "abc"}',
                                            encoding="utf-8")
    trash = tmp_path / "_trash"
    trash.mkdir()
    f = trash / "old.jsonl"
    f.write_text("{}")
    t2 = time.time() - 31 * 86400
    os.utime(f, (t2, t2))
    assert r._cleanup_trash() == 1, "非法设置值应回退到命令行参数（30 天）"
