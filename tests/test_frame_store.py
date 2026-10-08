"""列式帧存储（FrameStore）的正确性测试。

背景：原来把场次 jsonl 解析成 `list[dict]` 缓存在内存里，一场 217475 帧的
场次要 1550 MB（7467 字节/帧），两条缓存上限就是 3.1 GB —— 在一台 7.4 GB
且没有 swap 的机器上这是会把服务打死的量级。

改成列式存储 + 只读 Frame 视图后内存降到几十 MB。但换了存储就换了出错的
方式，所以这里逐帧逐字段把新旧两种解析结果对一遍：
  · 已存储字段的键存在性、值的**类型**与数值必须完全一致
    （含 int/float 之分 —— lap 读成 3.0 会让圈号显示成 3.0）
  · 「键缺席」与「值是 null」必须区分（dict 语义）
  · 全流程不能读到未存储的字段（读了会被记进 MISSED_FIELDS）
"""
import json
import threading
import time
import tracemalloc

import pytest

# 全库没有任何一处读、因此不该被存储的字段（存了纯属白占内存）
# ⚠️ wheel_rads 已从这份名单里移出：轮胎滑移检测（gt7analysis.wheel_slip）会读它。
NEVER_READ = ["tyre_temp", "tyre_press", "tyre_wear", "wheel_revs",
              "susp_height", "velocity", "seq", "position", "lap_count",
              "oil_pressure", "water_temp", "oil_temp", "hand_brake", "in_gear",
              "time_of_day", "turbo_boost", "num_cars"]
# 🔴 quali_pos 已被读（进站与名次卡 /pitstops：比赛中 0x84 = 当前名次），
#    2026-10-08 起存进 _FRAME_COL_KIND——别再挪回 NEVER_READ。

HEADER = {"session_id": "abcdef01", "circuit": "test", "car": 1302,
          "powertrain": "fuel", "started_at": 1000.0}


def _frame(lap, i, fps, t):
    """造一帧：字段齐全，含标量 / 数组 / 布尔 / 字符串 / null。"""
    ph = i / fps
    return {
        "t": round(t, 4), "seq": int(t * 1000), "lap": lap,
        "speed_kph": round(180.0 + 20 * ph, 2),
        "rpm": round(5000 + 1000 * ph, 1), "gear": 4,
        "throttle": round(max(0.0, 1 - 2 * ph), 3),
        "brake": round(max(0.0, 2 * ph - 1), 3),
        "g_force": [round(0.5 - ph, 3), round(ph - 0.5, 3), 0.0],
        "gas_level": round(60.0 - ph, 2), "gas_capacity": 100.0,
        "car_x": round(100 + i * 0.5, 4), "car_z": 50.0, "car_y": 3.5,
        "car_code": 2181, "layout": "A", "has_coords": True,
        "best_lap": 142155 if lap > 1 else None,
        # wheel_rads 是**已存储**字段（滑移检测要读）；其余几个全库没有一处读，
        # 写进来是为了证明「不存它们也没人读」
        "wheel_rads": [100.0, 100.0, 99.0, 99.0],
        "tyre_temp": [85.0, 86.0, 85.5, 86.5], "tyre_press": [2.1] * 4,
        "susp_height": [0.03] * 4,
        "velocity": [10.0, 0.0, 20.0], "oil_pressure": 6.9, "water_temp": 85.0,
        "position": None, "car_on_track": True, "num_cars": 20, "flags": 393,
    }


def _write_session(path, laps=3, fps=1200, hz=60.0, mutate=None):
    rows = []
    t = 1000.0
    for lap in range(1, laps + 1):
        for i in range(fps):
            f = _frame(lap, i, fps, t)
            if mutate:
                mutate(f, lap, i)
            rows.append(json.dumps(f, ensure_ascii=False))
            t += 1 / hz
    path.write_text(json.dumps(HEADER, ensure_ascii=False) + "\n"
                    + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _plain(path):
    """老口径：json 解析成 list[dict]（作为比对基准）。"""
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    return json.loads(lines[0]), [json.loads(x) for x in lines[1:] if x.strip()]


@pytest.fixture
def sess(dash, tmp_path):
    dash._FRAMES_CACHE.clear()
    dash.MISSED_FIELDS.clear()

    def mutate(f, lap, i):
        if lap == 2 and i == 5:
            del f["car_z"]              # 键缺席（不能算成 null）
        if lap == 3 and i == 7:
            # ⚠️ 只能用 car_code 这类真有可能是 null 的字段。
            #    别拿 speed_kph 试 —— analyze_session 里 max(speeds) 遇到 None
            #    会直接 TypeError（旧实现也一样，不是本次改动引入的），
            #    而真实遥测里速度从不为空，测它没有意义。
            f["car_code"] = None

    return _write_session(tmp_path / "20261008_010000_unknown_abcdef02.jsonl",
                          mutate=mutate)


class TestFidelity:
    """与旧的 list[dict] 逐帧逐字段比对 —— 这是本次重构的根本保证。"""

    def test_帧数与表头一致(self, dash, sess):
        header, store = dash._load_frames(sess)
        ref_header, ref = _plain(sess)
        assert header == ref_header
        assert len(store) == len(ref) == 3 * 1200

    def test_已存储字段逐帧完全一致(self, dash, sess):
        _, store = dash._load_frames(sess)
        _, ref = _plain(sess)
        miss = object()
        checked = 0
        for i, rf in enumerate(ref):
            fr = store[i]
            for k in dash._FRAME_COL_KIND:
                a = rf.get(k, miss)
                if a is miss:               # 基准里这帧就没这个键
                    assert k not in fr, f"第 {i} 帧 {k} 不该存在"
                    continue
                if a is None:
                    assert fr[k] is None, f"第 {i} 帧 {k} 应为 None，实际 {fr[k]!r}"
                elif isinstance(a, float):
                    b = fr[k]
                    # 用 hex 比：能抓住 float32 那类「看着差不多」的精度损失
                    assert b == a and float(b).hex() == a.hex(), \
                        f"第 {i} 帧 {k} 数值不等：{a!r} vs {b!r}"
                elif isinstance(a, list):
                    assert fr[k] == a, f"第 {i} 帧 {k} 不等：{a!r} vs {fr[k]!r}"
                else:
                    b = fr[k]
                    assert b == a and type(b) is type(a), \
                        f"第 {i} 帧 {k} 类型/值不符：" \
                        f"{a!r}({type(a).__name__}) vs {b!r}({type(b).__name__})"
                checked += 1
        assert checked > 0

    def test_整型字段不会被写成浮点(self, dash, sess):
        """lap/gear/car_code 是整数；读成 3.0 会让圈号显示成 3.0、
        还会让 car_name_of(2181.0) 查不到车型。"""
        _, store = dash._load_frames(sess)
        for f in (store[0], store[1500], store[-1]):
            for k in ("lap", "gear", "car_code"):
                assert isinstance(f[k], int), f"{k} 应是 int，实际 {type(f[k])}"
            for k in ("t", "speed_kph", "throttle", "gas_level"):
                assert isinstance(f[k], float), f"{k} 应是 float，实际 {type(f[k])}"

    def test_布尔与字符串类型不丢(self, dash, sess):
        _, store = dash._load_frames(sess)
        assert store[0]["has_coords"] is True
        assert store[0]["layout"] == "A"

    def test_键缺席与值为null要区分(self, dash, sess):
        _, store = dash._load_frames(sess)
        _, ref = _plain(sess)
        # 第 2 圈第 5 帧删掉了 car_z → 键缺席
        gi = 1200 + 5
        assert "car_z" not in ref[gi]
        assert "car_z" not in store[gi], "缺席的键不该被算成存在"
        with pytest.raises(KeyError):
            store[gi]["car_z"]
        assert store[gi].get("car_z") is None
        assert store[gi].get("car_z", "缺省") == "缺省"
        # 第 3 圈第 7 帧 car_code = None → 键存在但值是 null
        ni = 2400 + 7
        assert "car_code" in ref[ni] and ref[ni]["car_code"] is None
        assert "car_code" in store[ni], "值为 null 的键仍然算存在"
        assert store[ni]["car_code"] is None
        assert store[ni].get("car_code", "缺省") is None, \
            "键存在但值为 null 时 .get 应返回 None 而不是默认值"
        assert store[0].get("根本没有这个字段", "缺省") == "缺省"

    def test_整场都是null的字段(self, dash, tmp_path):
        """整列 null 用哨兵标记，不该为此存 21 万个下标。"""
        p = _write_session(tmp_path / "allnull.jsonl", laps=1, fps=200,
                           mutate=lambda f, lap, i: f.update({"car_code": None}))
        dash._FRAMES_CACHE.clear()
        _, store = dash._load_frames(p)
        assert store.n == 200
        assert store._null["car_code"] is dash._ALL, "整场 null 应走哨兵分支"
        assert store[0]["car_code"] is None
        assert store[199].get("car_code", "缺省") is None

    def test_切片与索引语义同list(self, dash, sess):
        _, store = dash._load_frames(sess)
        flat = [f["t"] for f in store]
        assert [f["t"] for f in store[10:13]] == flat[10:13]
        assert [f["t"] for f in store[::500]] == flat[::500]
        assert store[-1]["t"] == flat[-1]
        with pytest.raises(IndexError):
            store[len(store)]
        assert bool(dash.FrameStore.empty()) is False
        assert len(dash.FrameStore.empty()) == 0


class TestNoUnstoredFieldRead:
    """存储清单必须覆盖全流程真正读到的字段。"""

    def test_全流程没有读到未存储的字段(self, dash, sess):
        dash.MISSED_FIELDS.clear()
        dash._FRAMES_CACHE.clear()
        st = dash.analyze_session(sess)
        assert "error" not in st, st
        dash.compare_session(sess, ref_lap_no=1, cmp_lap_no=2)
        dash.race_line_session(sess, 1)
        dash.session_series(sess, None, 500)
        dash.session_series(sess, 1, 500)
        dash.session_frames(sess, 0, 50)
        dash.session_frames(sess, 0, 50, lap_no=1)
        dash.session_csv(sess)
        dash.session_csv(sess, lap_no=1)
        dash._valid_laps(sess)
        dash.build_session_page(sess, st)
        assert dash.MISSED_FIELDS == set(), (
            f"以下字段被读了但没存，请在 _FRAME_COL_KIND 里补上："
            f"{sorted(dash.MISSED_FIELDS)}")

    def test_没有白白存储没人读的字段(self, dash):
        for k in NEVER_READ:
            assert k not in dash._FRAME_COL_KIND, f"{k} 没有任何一处读，不该存"

    def test_读了没存的字段会被记录(self, dash, sess):
        """守卫本身要有效：故意读一个没存的字段，必须被记下来。"""
        dash.MISSED_FIELDS.clear()
        _, store = dash._load_frames(sess)
        assert store[0].get("tyre_temp") is None      # 拿到 None（不是真值）
        assert "tyre_temp" in dash.MISSED_FIELDS, "守卫漏报了"
        dash.MISSED_FIELDS.clear()


class TestMemory:
    def test_列式存储明显省内存(self, dash, tmp_path):
        p = _write_session(tmp_path / "mem.jsonl", laps=4, fps=1500)

        tracemalloc.start()
        dicts = _plain(p)[1]
        dict_mem = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()

        dash._FRAMES_CACHE.clear()
        tracemalloc.start()
        _, store = dash._load_frames(p)
        store_mem = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()

        assert len(dicts) == len(store) == 6000
        ratio = dict_mem / max(store_mem, 1)
        assert ratio > 4, (f"列式应比 list[dict] 省得多："
                           f"{store_mem/1024:.0f} KB vs {dict_mem/1024:.0f} KB"
                           f"（仅 {ratio:.1f} 倍）")


class TestSentinel:
    """0xFFFF 哨兵值归一（与 lap 同一个家族的老问题）。"""

    @pytest.mark.parametrize("raw,want", [
        (65535, 0), (65000, 0), (64999, 64999), (20, 20), (1, 1),
        (None, 0), (0, 0),
    ])
    def test_u16_归一(self, dash, raw, want):
        assert dash._u16(raw) == want

    def test_lap_no_复用同一口径(self, dash):
        assert dash._lap_no({"lap": 65535}) == 0
        assert dash._lap_no({"lap": 3}) == 3
        assert dash._lap_no({}) == 0

    def test_v1_live_里的哨兵被归一(self, dash):
        snap = {"connected": True, "powertrain": "fuel", "frames": 1,
                "latest": {"num_cars": 65535, "quali_pos": 65535,
                           "g_force": [0.1, 0.2, 0.0]},
                "grid_start": 65535}
        d = dash._v1_live(snap)
        assert d["car"]["race"]["num_cars"] == 0
        assert d["car"]["race"]["grid_position"] == 0
        assert d["car"]["race"]["grid_start"] == 0

    def test_v1_live_里的正常名次不受影响(self, dash):
        snap = {"connected": True, "powertrain": "fuel", "frames": 1,
                "latest": {"num_cars": 20, "quali_pos": 7,
                           "g_force": [0.1, 0.2, 0.0]},
                "grid_start": 12}
        d = dash._v1_live(snap)
        assert d["car"]["race"]["num_cars"] == 20
        assert d["car"]["race"]["grid_position"] == 7
        assert d["car"]["race"]["grid_start"] == 12

    def test_v1_live_history_里的圈号哨兵被归一(self, dash):
        """history[] 是逐帧的，菜单态的 0xFFFF 不能原样出现在公开接口里。"""
        snap = {"connected": True, "powertrain": "fuel", "frames": 2,
                "latest": {"g_force": [0.1, 0.2, 0.0]},
                "history": [{"lap": 65535, "t": 1.0}, {"lap": 3, "t": 1.1}]}
        d = dash._v1_live(snap)
        assert [h["lap"] for h in d["history"]] == [0, 3]

    def test_normalize_u16_frame_只归一已存在的键(self, dash):
        f = {"lap": 65535, "num_cars": 65535, "quali_pos": 65535,
             "speed_kph": 200.0}
        out = dash._normalize_u16_frame(f)
        assert out is f
        assert (out["lap"], out["num_cars"], out["quali_pos"]) == (0, 0, 0)
        assert out["speed_kph"] == 200.0        # 其余字段一个都不动

    def test_normalize_u16_frame_不凭空补键(self, dash):
        """「键缺席」不能被改写成「键在、值是 0」——那会改掉 `in` 的口径。"""
        f = {"lap": 3}
        dash._normalize_u16_frame(f)
        assert set(f) == {"lap"}

    def test_state_snapshot_的_latest_也被归一(self, dash, tmp_path):
        """/api/state 的 latest 是**原始帧**（不是 v1 结构），同样不能漏 65535。"""
        hub = dash.TelemetryHub(tmp_path / "status.json")
        hub.refresh = lambda: None              # 别去读真实状态文件
        hub._latest = {"lap": 65535, "num_cars": 65535, "quali_pos": 65535}
        latest = hub.snapshot(max_frames=10)["latest"]
        assert (latest["lap"], latest["num_cars"], latest["quali_pos"]) == (0, 0, 0)
        # 归一只能作用在**副本**上：hub 自己缓存的原始帧必须保持原样
        assert hub._latest["num_cars"] == 65535


class TestMemoConcurrency:
    """store._memo 跨请求共享（缓存的 store 会被多个线程同时用）。

    读-改-写不加锁：轻则一个慢接口被并发打进来时烧两遍 CPU（/events 冷 9s），
    重则 dict 在迭代中被另一个线程改（RuntimeError / 读到半成品条目）。
    """

    def test_命中时不重复计算(self, dash):
        store = dash.FrameStore.empty()
        calls = []
        assert store.memo_compute("k", lambda: calls.append(1) or [1, 2, 3]) \
            == [1, 2, 3]
        assert store.memo_compute("k", lambda: calls.append(1) or [1, 2, 3]) \
            == [1, 2, 3]
        assert len(calls) == 1

    def test_同一_key_并发只算一次(self, dash):
        """8 个线程同时要同一个 key：fn 只能跑一次。"""
        store = dash.FrameStore.empty()
        n, nlock = [0], threading.Lock()

        def fn():
            with nlock:
                n[0] += 1
            time.sleep(0.05)      # 放大「正在算」的窗口，把竞态逼出来
            return {"v": 42}

        out = []
        ts = [threading.Thread(target=lambda: out.append(
            store.memo_compute("k", fn))) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert len(out) == 8
        assert all(o == {"v": 42} for o in out)
        assert n[0] == 1         # 🔴 无锁 / 锁粒度不对时这里会是 8

    def test_不同_key_互不阻塞(self, dash):
        """正在算慢 key 时，另一个 key 不该被堵住（否则 /events 会卡住 /series）。"""
        store = dash.FrameStore.empty()
        store.memo_compute("fast", lambda: 1)     # 先缓存住快 key
        done = threading.Event()

        def slow():
            done.wait(2.0)
            return "slow"

        t = threading.Thread(target=lambda: store.memo_compute("slow", slow))
        t.start()
        time.sleep(0.05)                          # 确保慢 key 已进入 fn()
        t0 = time.time()
        assert store.memo_compute("fast", lambda: 0) == 1
        assert time.time() - t0 < 0.5             # 读别的 key 不该排队等 2 秒
        done.set()
        t.join()


class TestLiveCarUnits:
    """对外字段的命名/单位（v1 只加不改，所以错名字只能并肩加一个对的）。"""

    def test_wheel_rad_per_s_与旧键同值(self, dash):
        """wheel_rev_per_s 名不副实（写「转/秒」实为 rad/s）：
        新增正确命名的键，旧键保留同一取值，谁都不会被打破。"""
        snap = {"connected": True, "powertrain": "fuel", "frames": 1,
                "latest": {"wheel_rads": [100.0, 100.5, 99.0, 99.5],
                           "g_force": [0.1, 0.2, 0.0]}}
        car = dash._v1_live(snap)["car"]
        assert car["wheel_rad_per_s"] == [100.0, 100.5, 99.0, 99.5]
        assert car["wheel_rev_per_s"] == car["wheel_rad_per_s"]

    def test_缺_wheel_rads_时两键一起降级(self, dash):
        snap = {"connected": True, "powertrain": "fuel", "frames": 1,
                "latest": {"g_force": [0.1, 0.2, 0.0]}}
        car = dash._v1_live(snap)["car"]
        assert car["wheel_rad_per_s"] == []
        assert car["wheel_rev_per_s"] == []


class TestPresence:
    """`"k" in f` 走的是一张预先算好的存在位图，不是真的取一次值。

    这是一条为性能开的路，所以必须证明它和「取值看是否缺席」的老口径
    逐帧等价 —— 否则一次误判就会让某场次的圈数据整段消失。
    """

    def test_位图与取值口径逐帧一致(self, dash, sess):
        _, store = dash._load_frames(sess)
        cases = ["lap", "car_z", "car_code", "speed_kph", "g_force",
                 "layout", "tyre_temp", "根本没这个字段"]
        for k in cases:
            for i in (0, 5, 7, 1199, 1200, 1205, len(store) - 1):
                f = store[i]
                want = store._value(k, i) is not dash._ABSENT
                assert (k in f) == want, f"字段 {k} 第 {i} 帧判键不一致"

    def test_某帧缺键时判为不在(self, dash, sess):
        _, store = dash._load_frames(sess)
        assert "car_z" in store[0]
        assert "car_z" not in store[1200 + 5]        # 该帧被删掉了 car_z

    def test_场次里出现过但未存储的字段判为存在(self, dash, sess):
        # tyre_temp 文件里有、但不在存储清单里：读出来是 None，键算「在」
        _, store = dash._load_frames(sess)
        assert "tyre_temp" in store[0]

    def test_整场都没出现过的字段判为不在(self, dash, sess):
        _, store = dash._load_frames(sess)
        assert "根本没这个字段" not in store[0]

    def test_整场都缺席的字段判为不在(self, dash, tmp_path):
        # 全部帧都缺 layout → 缺席标记退化成一个哨兵，
        # 位图这条支路（_ALL）也要判对，不能一律 True。
        def mutate(f, lap, i):
            del f["layout"]

        p = _write_session(tmp_path / "absent.jsonl", laps=2, fps=50,
                           mutate=mutate)
        dash._FRAMES_CACHE.clear()
        _, store = dash._load_frames(p)
        assert not any("layout" in f for f in store)
        assert store.lap_frames(), "其它字段仍应正常"


    def test_get_与_getitem_跟参考实现等价(self, dash, sess):
        """Frame.get / Frame[k] 走的是「干净数字列」快路，必须与 _value 等价。

        快路为了速度绕开了 _value 的一整套判断，所以要逐帧逐字段把两边
        对一遍 —— 尤其是 get 的默认值语义和 getitem 的 KeyError 语义。
        """
        _, store = dash._load_frames(sess)
        sentinel = object()
        for k in ("lap", "car_z", "car_code", "speed_kph", "g_force",
                  "layout", "tyre_temp", "根本没这个字段"):
            for i in (0, 5, 7, 1200 + 5, len(store) - 1):
                f = store[i]
                ref = store._value(k, i)
                if ref is dash._ABSENT:
                    assert f.get(k, sentinel) is sentinel, f"{k}#{i} 该给默认值"
                    with pytest.raises(KeyError):
                        f[k]
                else:
                    got = f.get(k, sentinel)
                    assert type(got) is type(ref) and got == ref, f"{k}#{i} get"
                    got2 = f[k]
                    assert type(got2) is type(ref) and got2 == ref, f"{k}#{i} [k]"

    def test_快路标记只对干净数字列开(self, dash, sess):
        """快路一旦被误开，有缺席/null 的列会当作 0.0 返回 —— 这是静默错值。

        所以这里把「不该开」的几类都钉住：键缺席过、值为过 null、数组列、
        字符串列、以及本场次没存储的字段。
        """
        _, store = dash._load_frames(sess)
        assert store._col["speed_kph"][4] is True      # 干净数字列 → 开
        assert store._col["car_z"][4] is False         # 有一帧缺键
        assert store._col["car_code"][4] is False      # 有一帧是 null
        assert store._col["g_force"][4] is False       # 数组列
        assert store._col["layout"][4] is False        # 字符串列
        assert store._col["tyre_temp"][4] is False     # 未存储


class TestMemo:
    """每场只算一次的归算：结果必须与「每次重算」完全一致，且真的只算一次。"""

    def test_lap_frames_与逐帧筛选结果一致(self, dash, sess):
        _, store = dash._load_frames(sess)
        want = [f for f in store if "lap" in f]
        got = store.lap_frames()
        assert [f._i for f in got] == [f._i for f in want]

    def test_lap_frames_复用同一个列表(self, dash, sess):
        _, store = dash._load_frames(sess)
        assert store.lap_frames() is store.lap_frames()

    def test_valid_laps_记忆化且与重算一致(self, dash, sess):
        a = dash._valid_laps(sess)
        b = dash._valid_laps(sess)
        assert a is b, "应当直接复用上次的结果"
        laps, grouped, t0 = a
        assert laps and t0

        # 再和「照旧口径把全部帧喂 clean_laps」现算一遍比 —— 这是记忆化
        # 能成立的全部依据（clean_laps(lap_frames) == clean_laps(all)）。
        import gt7analysis
        _, store = dash._load_frames(sess)
        ref = gt7analysis.clean_laps(list(store))
        assert sorted(grouped) == sorted(ref)
        for k in ref:
            assert [f._i for f in grouped[k]] == [f._i for f in ref[k]]


class TestCleanLapsEquivalence:
    """把 clean_laps 的输入从「全部帧」换成「含 lap 的帧」必须毫无差别。

    这一条是 _valid_laps 记忆化的前提。构造的样本把 split_laps / clean_laps
    里所有会分流的边界都塞进去：缺 lap 字段、lap=0、菜单态 0xFFFF、
    以及一个距离过短要被剔掉的末圈。
    """

    def _pairs(self, dash, tmp_path):
        def mutate(f, lap, i):
            if lap == 1 and i < 3:
                del f["lap"]                 # 整帧没有 lap 字段
            if lap == 1 and i == 10:
                f["lap"] = 0                 # 开赛前
            if lap == 3 and i == 20:
                f["lap"] = 65535             # 菜单态哨兵
            if lap == 3:
                # 整圈都在滑行（不是只改前几帧 —— 圈距离得真的短到
                # < 45% 中位圈长，否则 clean_laps 不会剔除它）
                f["speed_kph"] = 2.0

        p = _write_session(tmp_path / "eq.jsonl", laps=3, fps=200, mutate=mutate)
        dash._FRAMES_CACHE.clear()
        _, store = dash._load_frames(p)
        return store

    def test_两种输入分组完全相同(self, dash, tmp_path):
        import gt7analysis
        store = self._pairs(dash, tmp_path)
        allf = gt7analysis.clean_laps(list(store))
        only = gt7analysis.clean_laps(store.lap_frames())
        assert sorted(allf) == sorted(only)
        for k in allf:
            assert [f._i for f in allf[k]] == [f._i for f in only[k]]

    def test_末圈假圈仍被剔除(self, dash, tmp_path):
        import gt7analysis
        store = self._pairs(dash, tmp_path)
        got = gt7analysis.clean_laps(store.lap_frames())
        assert 3 not in got, "滑行离场的末圈应仍被剔除"
        assert 1 in got and 2 in got
