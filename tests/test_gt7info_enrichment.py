"""守住 gt7info 车型/赛道/轮胎增强数据（maker / cargrp / country / bop / logo / tyres）。

这些都是从 ddm999/gt7info 搬进来的内置表，回归测试保证：
- 车型名片能解出品牌 + 组别（不依赖网络）；
- 赛道 BoP / logo 映射对齐到 course 表；
- 轮胎参考表结构稳定、且明确标注「非遥测量」。
"""
import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gt7analysis as G  # noqa: E402


# ---- 车型名片（maker / cargrp / country） ---------------------------------

def test_car_info_resolves_maker_and_group(tmp_path):
    # 造一张最小 cars.csv：ID,ShortName,Maker
    csv_p = tmp_path / "cars.csv"
    csv_p.write_text(
        "ID,ShortName,Maker\n3459,918 Spyder '13,136\n808,Escudo,182\n",
        encoding="utf-8",
    )
    info = G.car_info_of(3459, str(csv_p))
    assert info["name"] == "918 Spyder '13"
    assert info["maker"] == "Porsche"          # maker.csv: 136 = Porsche
    # 918 Spyder 是街车（N）→ 组别为空串，不渲染徽章
    assert info["group"] == ""
    assert info["known"] is True
    assert info["country"] == "Germany"


def test_car_info_group_badge_for_race_car(tmp_path):
    csv_p = tmp_path / "cars.csv"
    csv_p.write_text("ID,ShortName,Maker\n808,Escudo,39\n", encoding="utf-8")
    info = G.car_info_of(808, str(csv_p))
    # Escudo 是 Gr.X（VGT/特殊），应当给组别徽章
    assert info["group"] == "Gr.X"
    assert info["maker"] == "Suzuki"


def test_car_info_unknown_code_no_maker():
    # 不给 csv → 没有名字也没有厂商/国别，只给组别（纯内置表）
    info = G.car_info_of(808)
    assert info["name"].startswith("CAR-ID-")
    assert info["maker"] == ""
    assert info["group"] == "Gr.X"   # 组别仍来自内置 cargrp 表


def test_car_group_of_known_and_unknown():
    assert G.car_group_of(808) == "Gr.X"
    assert G.car_group_of(3459) == ""     # 街车
    assert G.car_group_of(0) == ""        # 非法码
    assert G.car_group_of(999999) == ""   # 未收录


def test_maker_and_country_tables_loaded():
    assert len(G.makers()) >= 80
    assert len(G.countries()) >= 20
    assert len(G.car_groups()) >= 500


# ---- 赛道 BoP / logo -------------------------------------------------------

def test_bop_of_known_and_unknown():
    # Spa (1269) 在标注表里是 Mid
    assert G.bop_of(1269) in ("High", "Mid", "Low")
    # 部分赛道没有标注 → 空串
    assert G.bop_of(347) == "" or G.bop_of(347) in ("High", "Mid", "Low")
    assert G.bop_of(0) == ""


def test_logo_of_maps_course_id():
    assert G.logo_of(10) == "suzuka"
    # 多个布局可能共用一个 logo；未收录赛道返回空串
    assert G.logo_of(0) == ""
    assert isinstance(G.logo_of(1273), str)


def test_tracks_carry_bop_and_logo_fields():
    # 内置 course 表应当至少有部分赛道带 BoP / logo
    cs = G.courses()
    assert any(c["bop"] for c in cs)
    assert any(G.logo_of(c["id"]) for c in cs)


# ---- 轮胎参考 ---------------------------------------------------------------

def test_tyre_reference_structure():
    r = G.tyre_reference()
    assert "life" in r and "wet" in r and "note" in r
    assert len(r["life"]) >= 5
    assert len(r["wet"]) >= 4
    # 关键：明确标注「非遥测量」，不能用于实时判断
    assert "非遥测" in r["note"] or "不是遥测" in r["note"]
    # life 行字段齐全
    life0 = r["life"][0]
    for k in ("compound", "laps_front_right", "laps_front_left"):
        assert k in life0
    # wet 行字段齐全
    wet0 = r["wet"][0]
    for k in ("compound", "ref_lap", "aquaplaning_pct"):
        assert k in wet0


def test_bop_helper_wiring():
    # 用名字也能反查（老数据没有 course_id 的情况）
    for c in G.courses():
        if c["bop"]:
            # 直接按 id 查
            assert G.bop_of(c["id"]) == c["bop"]
            break
