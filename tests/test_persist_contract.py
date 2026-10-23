"""落盘契约：接收器**采集到的每个字段都必须写进场次 jsonl**。

背景：用户反馈「场次里的数据太少，比如某时刻的时速/油门/刹车都没有」。
排查发现字段其实都在 TelemetrySample 里，但如果 to_json() 漏掉某个字段，
数据就永远不会出现在 jsonl 里——而且不会有任何报错，直到有人去翻文件
才发现「怎么少了一列」。所以把「dataclass 字段 ⊆ to_json 键」钉死成测试：
以后加了新字段忘了写进 to_json，这里立刻红。
"""
import dataclasses

# 详情页 / API 依赖的关键列，缺一个就会导致界面空列
REQUIRED = [
    "t", "speed_kph", "rpm", "gear", "throttle", "brake",
    "g_force", "gas_level", "gas_capacity", "lap",
    "tyre_temp", "tyre_press", "tyre_wear", "susp_height",
    "car_x", "car_z", "car_y", "velocity", "best_lap_ms", "last_lap_ms",
]


# 格式 C 独有字段：**只在 C 包时有值**，A/B/~ 包写 null 是纯浪费
# （60Hz × 4 个 null 字段 ≈ 每小时多 4MB，而它们永远是 null）。
# 所以这条契约要分两步守：默认构造时不要求有 key，但**有值时必须写出来**。
_CONDITIONAL_FIELDS = {"steer_angle_l", "steer_angle_r",
                       "wheel_base", "car_category",
                       # 格式 B/C 共有；surface 只有 C 有
                       "sway", "surge", "steer_wheel", "surface"}


def test_every_dataclass_field_is_persisted(rec):
    fields = [f.name for f in dataclasses.fields(rec.TelemetrySample)]
    keys = set(rec.TelemetrySample(t=1.0, seq=1).to_json().keys())
    missing = [f for f in fields
               if f not in keys and f not in _CONDITIONAL_FIELDS]
    assert not missing, f"这些字段采集了却没落盘，会在场次里凭空消失: {missing}"


def test_conditional_fields_are_written_when_present(rec):
    """有值时**必须**写出来 —— 这才是契约真正要防的"数据凭空消失"。"""
    s = rec.TelemetrySample(t=1.0, seq=1, steer_angle_l=0.05,
                            steer_angle_r=0.03, wheel_base=2.9,
                            car_category="GR1", sway=-6.0, surge=4.0,
                            steer_wheel=0.8, surface="TCTC")
    d = s.to_json()
    missing = [f for f in _CONDITIONAL_FIELDS if f not in d]
    assert not missing, f"这些字段有值却没落盘: {missing}"
    assert d["steer_angle_l"] == 0.05
    assert d["car_category"] == "GR1"
    assert d["surface"] == "TCTC"


def test_conditional_fields_absent_when_none(rec):
    """没有该字段时**不写** —— 而不是写 null。"""
    d = rec.TelemetrySample(t=1.0, seq=1).to_json()
    for f in _CONDITIONAL_FIELDS:
        assert f not in d, f"{f} 在 A 包里被写成了 null"


def test_required_columns_present(rec):
    keys = set(rec.TelemetrySample(t=1.0, seq=1).to_json().keys())
    missing = [k for k in REQUIRED if k not in keys]
    assert not missing, f"详情页/API 依赖的列缺失: {missing}"


def test_pedal_and_gfroce_are_numeric_not_none(rec):
    """踏板 / G 力必须是数：详情页的逐帧表与赛车线渐变直接拿它们算。"""
    s = rec.TelemetrySample(t=1.0, seq=1)
    j = s.to_json()
    for k in ("throttle", "brake"):
        assert isinstance(j[k], (int, float)), f"{k} 应是数字"
    assert isinstance(j["g_force"], list) and len(j["g_force"]) == 3
    assert isinstance(j["tyre_temp"], list) and len(j["tyre_temp"]) == 4
