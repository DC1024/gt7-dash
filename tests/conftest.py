"""加载带连字符文件名的模块（gt7-recorder.py / gt7-dashboard.py）。"""
import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # 让 tests 能 import 根目录的 gt7analysis

def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

@pytest.fixture(scope="session")
def rec():
    return load_module("gt7rec_under_test", "gt7-recorder.py")

@pytest.fixture(scope="session")
def dash():
    return load_module("gt7dash_under_test", "gt7-dashboard.py")

@pytest.fixture
def dec(rec):
    return rec.Decoder()

@pytest.fixture
def make_recorder(rec, tmp_path):
    def _make(**over):
        args = argparse.Namespace(
            output=str(tmp_path / "out"), ps5="auto", track_min_speed=15.0,
            track_min_frames=1, session_gap=300.0, heartbeat_interval=5.0,
            probe=False, status_file=None, verbose=False,
            decryptor="/app/gt7-decrypt", off_track_timeout=15.0)
        for k, v in over.items():
            setattr(args, k, v)
        return rec.Recorder(args)
    return _make
