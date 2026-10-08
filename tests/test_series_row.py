"""`_frame_row_at`（按列直读）× `_frame_row`（逐帧通用路径）的等价性。

背景：/csv 要给整场（实测 21 万帧）各出一行、每行取 10 个通道，表格分页
走同一条路。逐帧走 Frame.get 每行多两层函数调用，整场下来在慢机器上是
几百毫秒，于是加了「把 10 个通道的底层列一次抓齐、之后每行只做下标」的快路。

绕开 Frame 直读列之所以安全，是因为 _frame_row 里每个通道的兜底值都是 0.0
（`f.get(k) or 0.0`），而列式存储在「键缺席 / 值为 null」时填的正是 0.0。
但这是**推理**，推理会错 —— 所以这里逐行逐列把两条路对一遍。
"""
import json

# 与 tests/test_frame_store.py 的 fixture 同一套字段，但额外覆盖
# _frame_row 真正会读的那几个通道的空值分支。
BASE = {
    "t": 1000.0, "lap": 1, "speed_kph": 187.42, "rpm": 7321.5,
    "throttle": 0.815, "brake": 0.0, "gear": 5,
    "g_force": [0.312, -0.418, 0.02],
    "gas_level": 42.5, "gas_capacity": 100.0,
    "car_x": 12.5, "car_z": -30.0, "layout": "A", "has_coords": True,
}
HEADER = {"session_id": "abcdef01", "circuit": "test", "car": 1302,
          "powertrain": "fuel", "started_at": 1000.0}


def _write(path, rows):
    path.write_text(json.dumps(HEADER, ensure_ascii=False) + "\n"
                    + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                    + "\n", encoding="utf-8")
    return path


def _f(i, **over):
    r = dict(BASE)
    r["t"] = round(1000.0 + i / 60.0, 4)
    r.update(over)
    return r


def _normal_rows(n=40):
    return [_f(i) for i in range(n)]


def _sess_with_holes(tmp_path, name):
    """一份把 _frame_row 读到的每个通道的空值分支都覆盖到的场次。"""
    rows = []
    for i in range(60):
        r = _f(i)
        if i == 3:
            del r["speed_kph"]            # 键缺席
        if i == 4:
            r["rpm"] = None               # 值为 null
        if i == 5:
            r["throttle"] = None          # null（油门兜底 0.0）
        if i == 6:
            del r["brake"]                # 缺席（刹车兜底 0.0）
        if i == 7:
            r["gear"] = None              # null（档位 int(0)）
        if i == 8:
            r["gas_capacity"] = 0.0       # 纯电车口径 → 油量 None
        if i == 9:
            r["gas_level"] = None         # 油量 null
        if i == 10:
            del r["g_force"]              # 缺 G → [0,0,0]
        if i == 11:
            r["g_force"] = [0.5]          # G 只有 1 个分量 → 横向取 0.0
        if i == 12:
            del r["t"]                    # 时间缺席
        if i == 13:
            r["lap"] = 65535              # 菜单态哨兵 → 归一为 0
        if i == 14:
            r["lap"] = 0                  # 开赛前
        rows.append(r)
    # 再补一段正常圈，保证 clean_laps 不至于把整场判成假圈
    rows += [_f(i + 60, lap=2) for i in range(120)]
    return _write(tmp_path / name, rows)


def _each_row(dash, path):
    """两条路各出一套行，返回 [(帧号, 逐帧行, 直读行), ...]。"""
    dash._FRAMES_CACHE.clear()
    _, store = dash._load_frames(path)
    cols = dash._series_cols(store)
    assert cols is not None, "这份样本应当能走直读快路"
    out = []
    for i in range(len(store)):
        f = store[i]
        out.append((i, dash._frame_row(f, 0.0), dash._frame_row_at(cols, i, 0.0)))
    return out


class TestRowEquivalence:
    def test_有空值的场次逐行一致(self, dash, tmp_path):
        p = _sess_with_holes(tmp_path, "holes.jsonl")
        for i, slow, fast in _each_row(dash, p):
            assert fast == slow, (f"第 {i} 帧不一致\n  逐帧={slow}\n  直读={fast}")
            # 类型也要一致：CSV 里 float 与 int 的 str() 不同
            for a, b in zip(slow, fast):
                assert type(a) is type(b), f"第 {i} 帧类型不一致 {a!r} vs {b!r}"

    def test_正常场次逐行一致(self, dash, tmp_path):
        p = _write(tmp_path / "ok.jsonl", _normal_rows())
        for i, slow, fast in _each_row(dash, p):
            assert fast == slow, f"第 {i} 帧不一致"

    def test_菜单态哨兵在两条路都被归一(self, dash, tmp_path):
        p = _sess_with_holes(tmp_path, "holes2.jsonl")
        rows = _each_row(dash, p)
        assert rows[13][1][9] == 0 and rows[13][2][9] == 0     # lap 65535 → 0

    def test_纯电车油量两条路都是_None(self, dash, tmp_path):
        p = _sess_with_holes(tmp_path, "holes3.jsonl")
        rows = _each_row(dash, p)
        assert rows[8][1][8] is None and rows[8][2][8] is None

    def test_空存储走不了直读(self, dash):
        assert dash._series_cols(dash.FrameStore.empty()) is None

    def test_通道列缺失时退回逐帧实现(self, dash, tmp_path):
        """把某一列摘掉（模拟字段清单以后被人改动）必须退回逐帧。

        直读在缺列时静默给全 0，那是看不出错的假数据 —— 所以宁可慢，
        也要退回会显出真实语义的逐帧路径。
        """
        p = _write(tmp_path / "ok3.jsonl", _normal_rows())
        dash._FRAMES_CACHE.clear()
        _, store = dash._load_frames(p)
        assert dash._series_cols(store) is not None
        store._num.pop("speed_kph")             # 人为破坏：列不见了
        assert dash._series_cols(store) is None
        rb = dash._row_builder(store, 0.0)
        assert rb(store[0]) == dash._frame_row(store[0], 0.0)

    def test_整场没有_g_force_时两条路一致(self, dash, tmp_path):
        """G 通道整场缺席（arr 里没有 g_force 列）→ 横向/纵向都取 0.0。"""
        rows = [{k: v for k, v in _f(i).items() if k != "g_force"}
                for i in range(30)]
        p = _write(tmp_path / "nog.jsonl", rows)
        dash._FRAMES_CACHE.clear()
        _, store = dash._load_frames(p)
        cols = dash._series_cols(store)
        assert cols is not None
        for i in range(len(store)):
            assert (dash._frame_row_at(cols, i, 0.0)
                    == dash._frame_row(store[i], 0.0))
        assert dash._frame_row_at(cols, 0, 0.0)[6:8] == [0.0, 0.0]

    def test_row_builder_的快路选中直读(self, dash, tmp_path):
        p = _write(tmp_path / "ok2.jsonl", _normal_rows())
        dash._FRAMES_CACHE.clear()
        _, store = dash._load_frames(p)
        rb = dash._row_builder(store, 0.0)
        cols = dash._series_cols(store)
        assert rb(store[0]) == dash._frame_row_at(cols, 0, 0.0)
