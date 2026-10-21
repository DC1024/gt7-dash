"""圈间对比分析 —— 纯函数库（仅标准库），供仪表盘「对比分析」页与单测使用。

设计约定：
* 输入 frames 为 jsonl 解出的 dict 列表（至少含 t / speed_kph / lap）
* 距离轴：以每帧速度 × Δt 积分（从 0 起累计），是所有对齐的公共坐标
* 时间差定义：diff_ms = t_本圈(d) - t_参考圈(d)，**正值 = 本圈在该距离段丢时间**
"""
from __future__ import annotations

import bisect
import math
from pathlib import Path

# —— 车型解析 ——————————————————————————————

_CAR_CSV_CACHE: dict[str, dict[str, tuple[str, str]]] = {}


def _load_car_csv(path: str) -> dict[str, tuple[str, str]]:
    """读一张 `ID,ShortName,Maker` 表 → {ID: (车名, 厂商ID)}。读不到就是空表，不抛。

    🔴 车名里可能带逗号吗？社区表的 ShortName 实测不含逗号，所以直接
       split(',') 是安全的；真出现也只会截短，不会污染别的行。

    🔴 用 utf-8-sig 而不是 utf-8：补充表多半是玩家用 Excel 填的，Excel 存
       UTF-8 会带 BOM。用 utf-8 读的话首行 ID 变成 "\ufeffID"，isdigit()
       判不过 —— 症状是「明明填了却查不到」，很难往编码上想。

    第三列是**厂商 ID**（数字），不是厂商名：社区表只给 ID，名字要再查
    :data:`_MAKER_CSV`（见 :func:`car_info_of`）。
    """
    table: dict[str, tuple[str, str]] = {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh.read().splitlines()[1:]:
                seg = line.split(",")
                if len(seg) >= 2 and seg[0].strip().isdigit():
                    table[seg[0].strip()] = (
                        seg[1].strip(),
                        seg[2].strip() if len(seg) > 2 else "",
                    )
    except OSError:
        pass
    return table


def _extra_path_for(csv_path: str | None) -> str | None:
    """社区表旁边那张手工补充表的路径（同目录 `cars_extra.csv`）。"""
    if not csv_path:
        return None
    return str(Path(csv_path).with_name("cars_extra.csv"))


def _car_table(csv_path: str | None, extra_path: str | None
               ) -> dict[str, tuple[str, str]]:
    """社区表 + 补充表合并后的 {ID: (车名, 厂商ID)}，按路径缓存只读一次。

    🔴 合并是**逐列**的，不是整行覆盖：玩家在 `cars_extra.csv` 里往往只填
       车名、厂商列留空，整行覆盖会把社区表查到的厂商信息抹成空。
    """
    key = f"{csv_path or ''}|{extra_path or ''}"
    if key not in _CAR_CSV_CACHE:
        table: dict[str, tuple[str, str]] = {}
        if csv_path:
            table.update(_load_car_csv(csv_path))
        if extra_path:
            for cid, row in _load_car_csv(extra_path).items():
                base = table.get(cid, ("", ""))
                table[cid] = (row[0] or base[0], row[1] or base[1])
        _CAR_CSV_CACHE[key] = table
    return _CAR_CSV_CACHE[key]


def _code_key(code: object) -> str:
    """carCode → 查表用的键。

    🔴 记录器早期把 car_code 写成浮点（2181.0），`str()` 出来是 "2181.0"，
       查任何表都查不到 —— 症状是「车明明在表里却显示 CAR-ID-2181」。
       统一过一次 int 就不会再踩。
    """
    try:
        return str(int(code))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(code)


def car_name_of(code: int, csv_path: str | None = None,
                extra_path: str | None = None) -> str:
    """carCode → 车名。表来自 helper/download_cars_csv.py 下载的社区 CSV
    （格式 `ID,ShortName,Maker`，首行表头）。无表或未命中返回 CAR-ID-xxx。

    **补充表优先**：同目录下的 `cars_extra.csv`（没有就自动按 `<csv目录>/cars_extra.csv`
    找）会先查。这是给「社区表还没收录的车」留的手工入口 —— 例如 3656
    （Xiaomi Vision Gran Turismo）在社区表里查不到，玩家自己标上就能显示正名。

    结果按 csv 路径缓存，多次调用只读一次文件。
    """
    if not code or code <= 0:
        return ""
    ex = extra_path if extra_path is not None else _extra_path_for(csv_path)
    row = _car_table(csv_path, ex).get(_code_key(code))
    return row[0] if row and row[0] else f"CAR-ID-{_code_key(code)}"


# ===========================================================================
# 车型增强表（内置，来自 ddm999/gt7info）
# ===========================================================================
#
# 三张小表，都是**内置**而不是运行时下载（跟 `_COURSE_CSV` 一个道理：跑在
# 纯内网也要能出结果，而且加起来才几 KB）：
#   * `_MAKER_CSV`    —— 厂商 ID → 品牌名 + 国别 ID
#   * `_COUNTRY_CSV`  —— 国别 ID → 国家名 + 国旗码
#   * `_CAR_GROUP_CSV`—— 车 ID → 赛事组别（Gr.1~Gr.4 / Gr.B / Gr.X / N 街车）
#
# 🔴 车 → 厂商的**对应关系**在 `cars.csv`（运行时下载的社区表）里，那里的
#    Maker 列是**数字 ID**，必须再过一遍 `_MAKER_CSV` 才是人能看的品牌名。
#    两张表缺任何一张都只会少显示品牌，不会让车名消失。
#
# 🔴 组别里 `N` = 街车（没有 Gr. 前缀），`X` = GT7 的 **Gr.X**（VGT 概念车、
#    方程式、经典赛车、安全车等）。`N` 刻意翻译成空串：给一台 86 打上
#    「Gr.N」的徽章是错的，游戏里没这个组别。
_MAKER_CSV = """\
ID,Name,Country
3,Alfa Romeo,7
4,Aston Martin,4
5,Audi,5
6,BMW,5
7,Chevrolet,3
9,Citroen,6
10,Daihatsu,2
11,Dodge,3
12,Fiat,7
13,Ford,3
15,Honda,2
16,Hyundai,9
17,Jaguar,4
18,Lancia,7
21,Mazda,2
22,Mercedes-Benz,5
25,Mitsubishi,2
28,Nissan,2
30,Pagani,7
32,Peugeot,6
33,Gran Turismo,0
34,Renault,6
35,RUF,5
36,Shelby,3
38,Subaru,2
39,Suzuki,2
43,Toyota,2
44,TVR,4
46,Volkswagen,5
49,Infiniti,3
50,Lexus,2
51,MINI,5
52,Pontiac,3
55,Plymouth,3
57,Autobianchi,7
59,Amuse,2
65,DMC,3
69,Volvo,12
80,Caterham,4
86,Alpine,6
87,RE Amemiya,2
93,Chaparral,3
110,Ferrari,7
112,Lamborghini,7
113,Bugatti,5
114,Renault Sport,6
116,Maserati,7
117,McLaren,4
119,Tesla,3
121,KTM,15
125,Abarth,7
127,SRT,3
134,Zagato,7
135,Italdesign,7
136,Porsche,5
138,Fittipaldi Motors,7
139,GT Awards (SEMA),0
140,De Tomaso,7
141,Radical,4
143,Super Formula (Dallara),2
144,BAC,4
145,Willys,3
146,Jeep,3
147,Chris Holstrom Concepts,3
148,Eckert's Rod & Custom,3
149,Greddy,2
150,Wicked Fabrication,3
151,Roadster Shop,3
152,Genesis,9
153,AMG,5
154,DS AUTOMOBILES,6
155,NISMO,2
156,Greening Auto Company,3
157,Garage RCR,3
158,BVLGARI,7
159,Skoda,19
160,AFEELA,2
161,Opel,5
162,Mine's,2
163,Polestar,12
164,Xiaomi,20
165,Yangwang,20
"""

_COUNTRY_CSV = """\
ID,Name,Code
0,Others,pdi
2,Japan,jp
3,United States,us
4,United Kingdom,gb
5,Germany,de
6,France,fr
7,Italy,it
8,Australia,au
9,Korea,kr
10,Belgium,be
12,Sweden,se
13,Spain,es
14,Canada,ca
15,Austria,at
16,Switzerland,ch
17,Brazil,br
18,Croatia,hr
19,Czech Republic,cz
20,China,cn
21,UAE,ae
"""

_CAR_GROUP_CSV = """\
ID,Group
24,N
31,N
36,N
37,N
41,N
48,N
63,N
78,N
82,N
102,N
104,N
105,B
135,N
137,N
140,3
145,N
173,N
187,N
201,N
203,N
204,N
205,N
207,N
210,N
211,N
216,2
293,N
296,1
301,N
315,N
334,N
345,N
365,4
374,N
379,N
387,N
396,N
451,N
485,3
489,N
514,N
533,N
543,N
575,N
604,N
665,N
688,N
709,N
773,N
779,N
781,B
808,X
810,N
818,N
821,N
829,N
836,N
837,N
843,N
919,N
931,N
942,N
954,1
959,N
998,1
1027,N
1040,3
1044,N
1067,1
1069,X
1365,N
1370,N
1373,N
1378,N
1384,N
1385,N
1399,N
1402,N
1409,N
1410,N
1425,3
1426,N
1431,N
1433,N
1448,N
1458,N
1461,N
1466,2
1470,3
1474,N
1480,N
1481,N
1484,N
1504,N
1507,N
1508,N
1510,2
1516,2
1523,N
1527,N
1528,N
1536,X
1537,N
1539,N
1540,N
1541,N
1542,N
1543,N
1545,N
1549,N
1551,X
1553,X
1562,N
1563,4
1565,X
1578,N
1581,N
1582,N
1645,X
1646,1
1671,N
1689,N
1722,N
1729,N
1746,N
1770,N
1773,N
1778,N
1796,N
1797,3
1893,N
1895,N
1896,X
1898,N
1900,N
1902,3
1904,3
1905,3
1907,N
1916,N
1925,N
1926,X
1927,B
1931,N
1932,N
1933,N
1935,N
1956,N
1965,1
1973,N
1975,N
1984,3
1985,N
1986,X
1987,N
1990,N
2010,X
2011,N
2017,N
2018,N
2026,N
2049,N
2050,N
2051,N
2055,X
2059,X
2060,X
2074,N
2076,X
2077,X
2078,X
2080,X
2087,X
2095,X
2098,X
2099,X
2101,1
2103,X
2106,X
2107,X
2108,X
2109,X
2110,X
2111,X
2112,X
2113,X
2116,X
2117,X
2118,X
2119,X
2120,X
2121,X
2122,X
2123,X
2124,X
2127,1
2131,3
2134,X
2135,X
2136,N
2138,N
2139,N
2141,N
2142,X
2143,3
2144,N
2145,N
2146,N
2147,N
2148,N
2149,N
2150,N
2152,N
2153,N
2154,N
2155,N
2156,X
2157,4
2158,3
2159,3
2160,3
2161,4
2162,N
2163,4
2164,4
2166,4
2167,N
2169,N
2170,N
2171,N
2172,N
2173,N
2174,N
2175,N
2176,N
2177,3
2178,N
2179,1
2180,1
2181,1
2182,3
2183,3
2184,3
2185,3
2186,3
2187,3
2188,3
2190,3
2192,3
3183,3
3185,3
3187,1
3188,1
3192,4
3209,X
3210,X
3214,N
3215,N
3216,X
3217,N
3218,3
3219,N
3220,N
3221,3
3222,N
3223,3
3224,3
3225,X
3227,N
3228,3
3229,B
3230,B
3231,4
3232,B
3234,B
3235,3
3237,3
3238,3
3239,B
3241,B
3242,B
3245,4
3246,4
3247,4
3248,4
3249,4
3251,4
3252,4
3253,4
3254,4
3256,4
3257,4
3258,4
3259,4
3260,4
3261,4
3262,4
3263,4
3264,B
3265,B
3266,X
3267,N
3268,N
3295,N
3296,N
3297,X
3298,4
3299,N
3300,N
3301,N
3303,N
3304,N
3305,3
3306,N
3309,N
3310,4
3311,3
3312,1
3313,1
3314,1
3315,X
3316,X
3332,X
3333,1
3334,1
3335,1
3336,N
3337,N
3338,N
3339,X
3340,3
3341,N
3342,X
3343,N
3344,N
3345,N
3346,N
3348,2
3349,2
3350,2
3351,X
3352,3
3353,N
3354,N
3356,N
3357,N
3358,N
3359,N
3360,N
3361,N
3362,N
3363,X
3364,N
3365,N
3367,N
3368,N
3369,N
3370,N
3371,X
3372,X
3373,1
3374,X
3375,N
3376,X
3377,N
3383,N
3384,N
3385,N
3387,N
3388,X
3389,N
3390,X
3391,N
3392,N
3393,N
3394,3
3396,X
3397,2
3398,N
3399,4
3400,N
3401,N
3402,N
3403,N
3404,N
3405,3
3406,X
3407,X
3408,X
3409,N
3410,X
3411,N
3412,N
3413,N
3414,X
3415,N
3416,N
3417,X
3418,N
3419,3
3420,N
3421,N
3422,N
3423,N
3424,N
3426,N
3427,N
3428,N
3429,N
3430,N
3431,N
3432,N
3433,N
3434,N
3436,3
3437,N
3438,N
3439,N
3441,X
3442,N
3443,N
3444,N
3445,N
3446,X
3449,N
3450,3
3451,N
3452,X
3453,N
3454,N
3456,N
3457,X
3458,N
3459,N
3462,N
3464,N
3466,N
3467,N
3469,N
3471,N
3473,N
3474,3
3475,X
3476,X
3477,4
3478,X
3479,X
3480,4
3481,N
3482,N
3483,N
3485,N
3486,N
3487,N
3488,N
3489,X
3490,N
3493,3
3494,N
3495,N
3499,1
3500,N
3501,4
3502,3
3503,3
3504,N
3505,N
3506,N
3507,X
3508,3
3509,N
3510,X
3511,X
3512,N
3513,N
3514,N
3515,1
3517,X
3518,N
3519,N
3520,N
3521,N
3522,N
3523,2
3524,N
3525,X
3526,X
3528,X
3529,X
3530,N
3531,3
3532,N
3533,N
3534,X
3535,N
3536,N
3538,N
3539,N
3540,X
3541,X
3542,X
3543,X
3545,N
3546,X
3547,N
3548,N
3549,N
"""

#: 组别代号 → 显示文本。`N`（街车）不在里面，返回空串。
_GROUP_LABEL = {"1": "Gr.1", "2": "Gr.2", "3": "Gr.3", "4": "Gr.4",
                "B": "Gr.B", "X": "Gr.X"}

_MAKER_CACHE: dict[str, tuple[str, str]] | None = None
_COUNTRY_CACHE: dict[str, tuple[str, str]] | None = None
_GROUP_CACHE: dict[str, str] | None = None


def _parse_id_name(txt: str) -> dict[str, tuple[str, str]]:
    """解析 `ID,Name[,Extra]` 形式的内置表 → {ID: (Name, Extra)}。"""
    out: dict[str, tuple[str, str]] = {}
    for ln in txt.strip().splitlines()[1:]:
        p = ln.split(",")
        if len(p) >= 2 and p[0].strip():
            out[p[0].strip()] = (p[1].strip(),
                                 p[2].strip() if len(p) > 2 else "")
    return out


def makers() -> dict[str, tuple[str, str]]:
    """厂商 ID → (品牌名, 国别ID)。"""
    global _MAKER_CACHE
    if _MAKER_CACHE is None:
        _MAKER_CACHE = _parse_id_name(_MAKER_CSV)
    return _MAKER_CACHE


def countries() -> dict[str, tuple[str, str]]:
    """国别 ID → (国家名, 国旗码)。"""
    global _COUNTRY_CACHE
    if _COUNTRY_CACHE is None:
        _COUNTRY_CACHE = _parse_id_name(_COUNTRY_CSV)
    return _COUNTRY_CACHE


def car_groups() -> dict[str, str]:
    """车 ID → 组别代号（"1".."4" / "B" / "X" / "N"）。"""
    global _GROUP_CACHE
    if _GROUP_CACHE is None:
        _GROUP_CACHE = {k: v[0] for k, v in _parse_id_name(_CAR_GROUP_CSV).items()}
    return _GROUP_CACHE


def car_group_of(code: int) -> str:
    """carCode → 组别徽章文本（"Gr.3" / "Gr.X" …）。

    街车（`N`）与未收录的车返回 **空串** —— 调用方拿到空串就不要渲染徽章，
    别自己补一个 "Gr.N" 上去（游戏里没这个组别）。
    """
    if not code or code <= 0:
        return ""
    return _GROUP_LABEL.get(car_groups().get(_code_key(code), ""), "")


def car_info_of(code: int, csv_path: str | None = None,
                extra_path: str | None = None) -> dict:
    """carCode 的"名片"：车名 + 品牌 + 国别 + 组别，一次查全。

    返回 ::

        {"code": 3459, "name": "918 Spyder '13", "maker": "Porsche",
         "country": "Germany", "country_code": "de", "group": "Gr.3",
         "known": True}

    * `known` = 车名是不是查表查到的（`False` 时 name 形如 `CAR-ID-3656`）。
      前端想对"未识别车"单独提示就看它，别去字符串匹配 "CAR-ID-"。
    * 厂商/国别/组别都可能为空（社区表缺列、街车、未收录），**不是错误**。
    """
    if not code or code <= 0:
        return {"code": 0, "name": "", "maker": "", "country": "",
                "country_code": "", "group": "", "known": False}
    ex = extra_path if extra_path is not None else _extra_path_for(csv_path)
    row = _car_table(csv_path, ex).get(_code_key(code))
    name = row[0] if row and row[0] else ""
    maker_id = row[1] if row else ""
    maker_name, country_id = makers().get(maker_id, ("", ""))
    cname, ccode = countries().get(country_id, ("", ""))
    return {
        "code": int(code),
        "name": name or f"CAR-ID-{_code_key(code)}",
        "maker": maker_name,
        "country": cname,
        "country_code": ccode,
        "group": car_group_of(code),
        "known": bool(name),
    }



# —— 基础：分组与采样 ——————————————————————————————

def split_laps(frames: list[dict]) -> dict[int, list[dict]]:
    """按圈号分组（保持帧顺序），lap<=0 的帧丢弃。

    ⚠️ 仅按 lap 分组，**不**剔除首/末假圈。需要剔除前圈/末圈
    （开局静止、完赛离场）请用 :func:`clean_laps`。
    """
    laps: dict[int, list[dict]] = {}
    for f in frames:
        lap = f.get("lap") or 0
        if lap <= 0:
            continue
        laps.setdefault(lap, []).append(f)
    return laps


def _lap_peak_kph(frames: list[dict]) -> float:
    """一圈内所有帧的速度峰值 (km/h)。"""
    return max((f.get("speed_kph") or 0.0) for f in frames)


def _lap_distance_m(frames: list[dict]) -> float:
    """一圈内累计行驶距离（米），按速度 × Δt 积分。

    dt 异常（<=0 或 >1s）的区间不计距离，与 lap_samples 一致。
    ⚠️ 只作为**没有坐标时**的兜底：时间戳异常/丢包会让它偏离真实圈长
    （实测被放大约 40%）。有坐标时请用 `_lap_geo_length`。
    """
    dist = 0.0
    prev: dict | None = None
    for f in frames:
        if prev is not None:
            dt = (f.get("t") or 0.0) - (prev.get("t") or 0.0)
            if 0.001 < dt < 1.0:
                dist += (prev.get("speed_kph") or 0.0) / 3.6 * dt
        prev = f
    return dist


# 单帧位移超过这个数（>3600 km/h）判为坐标跳变（撞车回放/传送），不计入弧长
_LAP_GEO_GAP_M = 100.0


def _lap_geo_length(frames: list[dict]) -> float:
    """一圈的**几何弧长**（相邻帧 car_x/car_z 的弦长之和，米）。

    🔴 为什么需要它：速度积分 `Σ v·Δt` 依赖时间戳，丢包/时钟跳变会把圈长
       放大（实测 5078m 的赛道积出 7066m，进而把完整圈判成残圈）。
       几何弧长只依赖坐标，不受时间戳影响。

    坐标缺失的帧跳过；单帧位移 >100m 判为跳变跳过；有效点 <10 返回 0
    （调用方回退速度积分）。
    """
    total = 0.0
    prev: tuple[float, float] | None = None
    n = 0
    for f in frames:
        x, z = f.get("car_x"), f.get("car_z")
        if x is None or z is None:
            prev = None
            continue
        if prev is not None:
            d = math.hypot(x - prev[0], z - prev[1])
            if d < _LAP_GEO_GAP_M:
                total += d
        prev = (x, z)
        n += 1
    return total if n >= 10 else 0.0


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# —— 圈完整性：距离口径 ————————————————————————————
#
# 为什么不看时长：同一赛道换台车、下雨、堵车，圈速能差 20 秒；距离不会。
# 为什么不读赛道库：新赛道可能还没识别，而且 tracks.json 的 ref_len_m 本身
#   也是同一套速度积分算出来的，拿它当基准是循环论证。
#
# 上下界都是**实测分布里的空档**（6 场 57 圈，2026-10-21）：
#   残圈 / 中途退出：3.4% ~ 61.2%
#   完整圈：        93.4% ~ 117%（117% 是滚动发车的出场圈）
#   时钟跳变假圈：  149% ~ 362%（主机休眠，同一文件塞了两场比赛）
# 0.90 落在 61→93 的空档，1.25 落在 117→149 的空档。
_LAP_DIST_LO = 0.90
_LAP_DIST_HI = 1.25
# 圈数少于这个数时中位数不可信：2 圈时它就是两者的平均值，
# 一个残圈就能把正常圈顶成 182%。
_LAP_MEDIAN_MIN_N = 3


def _reference_lap_dist(dists: dict[int, float]) -> float:
    """从各圈距离里估出「这条赛道一圈有多长」（残圈判据的基准）。

    🔴 为什么不能直接用中位数：真实数据里出现过 4 圈的场次，其中 2 圈是
       时钟跳变的假圈（149% / 304%）、1 圈是 3% 的碎片，中位数被抬到 124%，
       反而把唯一正常的 100% 圈压成 80% 判成残圈。
       中位数只有在「一半以上是正常圈」时才稳，4 圈里 3 圈异常就失效。

    做法：
      1. 先找**最大的「同长度簇」**——以每个值为中心，±10% 内的成员数最多者。
         正常圈彼此只差 1~3%，很容易成簇；假圈各不相同的长度落单。
      2. 簇里 ≥2 圈 → 取簇内中位数（对 1~2% 的丢包误差免疫）。
      3. 一个簇都没有（每圈长度都不同）→ 取「非碎片里最短的」：
         时钟跳变/多场合并只会让距离**变长**，碎片只会更短，
         所以最短的那个完整候选就是赛道长度。

    圈数 <3 时返回 0.0：样本太少，判不了。
    """
    vals = [v for v in dists.values() if v > 0]
    if len(vals) < _LAP_MEDIAN_MIN_N:
        return 0.0

    best_center = 0.0
    best_members: list[float] = []
    for c in vals:
        members = [v for v in vals if 0.90 * c <= v <= 1.10 * c]
        if (len(members) > len(best_members)
                or (len(members) == len(best_members) and c < best_center)):
            best_center, best_members = c, members
    if len(best_members) >= 2:
        return _median(best_members)

    non_frag = [v for v in vals if v >= 0.30 * max(vals)]
    return min(non_frag) if non_frag else _median(vals)


def official_lap_times(laps: dict[int, list[dict]]) -> dict[int, float]:
    """从帧里的 `last_lap_ms` 还原每圈的**官方用时**（秒）。

    🔴 语义：`lap` 变成 N 的**第一帧**，其 `last_lap_ms` 是第 N-1 圈的官方
       用时（冲线后游戏才把圈号 +1）。所以第 N 圈的值要去第 N+1 圈的首帧找。

    🔴 为什么需要它：帧跨度 = 圈首帧到圈末帧的**墙上时间**，会把暂停、进站
       的等待算进圈速（实测第 11 圈 182s，官方 127s）。GT7 自己的计时不含
       暂停，`last_lap_ms` 才是用户在游戏里看到的数字。

    最后一圈没有「下一圈」可查（比赛结束/录制中断）→ 不返回，调用方回退
    帧跨度。只接受 20s~1h 的值，0 / 0xFFFFFFFF / 菜单态垃圾值都跳过。
    """
    ordered = sorted(laps)
    out: dict[int, float] = {}
    for i in range(len(ordered) - 1):
        n, nxt = ordered[i], ordered[i + 1]
        frames = laps.get(nxt) or []
        # 冲线后可能有一两帧还没更新 last_lap，往后看几帧
        for f in frames[:5]:
            ms = f.get("last_lap_ms")
            if isinstance(ms, (int, float)) and 20_000 <= ms <= 3_600_000:
                out[n] = round(float(ms) / 1000.0, 3)
                break
    return out


def lap_distances(laps: dict[int, list[dict]]) -> tuple[dict[int, float], float]:
    """每圈距离 + 基准圈长（残圈判据的唯一事实源）。

    距离优先用**几何弧长**（坐标弦长），它不受时间戳/丢包影响；
    整场**所有圈**都有有效坐标时才用几何，否则整体回退速度积分 ——
    避免同一场里两种口径混用，把基准圈长拉歪。

    返回 ``(dists, ref)``。``ref == 0.0`` 表示圈数太少（<3），基准不可用，
    调用方应退回「只判时长」的旧口径，不要拿它做距离判断。
    """
    geo = {n: _lap_geo_length(fs) for n, fs in laps.items()}
    use_geo = bool(laps) and all(v > 0 for v in geo.values())
    if use_geo:
        dists = dict(geo)
    else:
        dists = {n: _lap_distance_m(fs) for n, fs in laps.items()}
    return dists, _reference_lap_dist(dists)


def lap_is_complete(dist: float, med: float,
                    lo: float = _LAP_DIST_LO, hi: float = _LAP_DIST_HI) -> bool:
    """这一圈是否跑完了整条赛道（距离落在基准圈长的 [lo, hi] 区间）。

    🔴 上界与下界同样重要：
       · 下界挡「中途退出 / 刚进圈」的残圈；
       · 上界挡「主机休眠后两场写进同一文件」和时钟跳变造成的假圈
         （实测 149%~362%）。只卡下界会让它们通过。

    ``ref <= 0``（圈数 <3，判不了）返回 True —— **判不了就别误杀**，
    与修复前的行为保持一致。
    """
    if med <= 0:
        return True
    return lo * med <= dist <= hi * med


def clean_laps(frames: list[dict],
               laps: dict[int, list[dict]] | None = None) -> dict[int, list[dict]]:
    """按圈分组并剔除假圈（保持帧顺序）。

    假圈都源于 GT7 的 lap 字段语义：
      1. 菜单态：lap 为 0xFFFF(65535) 等特殊大值，直接丢弃。
      2. 首圈（比赛开始前，lap=0）与末圈（完赛后离场，末尾 lap=N）：
         这两段**根本没跑完一整条赛道**，是「假圈」。

    🔑 主判据 = **圈距离**（速度积分，单位米）。
       真实圈跑完一整条赛道，各圈距离高度一致（实测同场 5400~5800m）；
       假圈的距离只有几百米（前圈静止/末圈滑行离场）。
       取各圈距离的**基准圈长** m（见 _reference_lap_dist：最大同长簇的中位数，
       无簇时取非碎片里最短的），落在 [0.90m, 1.25m] 之外的判为假圈
       （判据见 lap_is_complete：下界挡残圈，上界挡休眠/时钟跳变）。
       ⚠️ 圈数 <3 时基准不可用，退回旧的「<45% 中位数」弱判据。

    辅助判据 = 峰值速度：某圈几乎全静止（峰值 < 30% × 全场最高峰值）也是假圈。

    ⚠️ 只剔除**首圈与末圈**：中间圈即使跑得慢（事故/慢速圈）也保留，
       避免误删用户的真实数据。单圈场次不剔除（首尾同一圈）。
    """
    laps = split_laps(frames) if laps is None else dict(laps)
    if not laps:
        return laps
    # 菜单态（0xFFFF 等特殊值）视为无效圈
    laps = {n: fs for n, fs in laps.items() if n < 65000}
    if not laps:
        return laps
    ordered = sorted(laps)
    if len(ordered) < 2:                 # 单圈：首尾同一圈，不剔
        return laps

    dists, med_d = lap_distances(laps)
    peaks = {n: _lap_peak_kph(fs) for n, fs in laps.items()}
    max_p = max(peaks.values())

    first, last = ordered[0], ordered[-1]
    out = dict(laps)
    for n in (first, last):
        if med_d > 0:
            # 距离偏离中位圈长太多 → 没跑完整圈（残圈 / 多场合并）
            short = not lap_is_complete(dists[n], med_d)
        else:
            # 圈数 <3：中位数不可信，退回弱判据（只挡「几乎没跑」的残片）
            med_all = _median(list(dists.values()))
            short = med_all > 0 and dists[n] < 0.45 * med_all
        # 几乎全静止 → 排队 / 停车场
        stalled = max_p > 0 and peaks[n] < 0.30 * max_p
        if short or stalled:
            del out[n]
    return out


def lap_samples(frames: list[dict], geo: bool | None = None) -> list[dict]:
    """一圈的帧 → 累计距离采样点。

    每点：dist(米) / t_rel(圈内秒) / v(m/s) / speed_kph / throttle / brake /
          glong(纵向G) / gmag(合成 G 大小) / x / z。

    🔴 距离轴优先用**几何弧长**：速度积分依赖时间戳，丢包/时钟跳变会把
       距离放大（实测 5078m 的圈积出 7066m）。只有几何与积分在同一量级
       （比值 0.5~2.0）时才信几何 —— 合成数据/坐标缩放异常时自动回退，
       不会因为坐标单位不同把整条距离轴缩小。

    `geo` 可选：True 强制几何（有坐标时）、False 强制积分、None=自动。
    `lap_profile` 的「积分 vs 几何漂移」诊断必须传 False，否则两者相同。
    """
    pts: list[dict] = []
    dist = 0.0
    geo_total = 0.0
    geo_vals: list[float] = []
    prev = None
    prev_xyz = None
    for f in frames:
        t = f.get("t") or 0.0
        v = (f.get("speed_kph") or 0.0) / 3.6
        if prev is not None:
            dt = t - prev["t"]
            if 0.001 < dt < 1.0:
                dist += prev["v"] * dt
        x, z = f.get("car_x"), f.get("car_z")
        if x is not None and z is not None:
            if prev_xyz is not None:
                d = math.hypot(x - prev_xyz[0], z - prev_xyz[1])
                if d < _LAP_GEO_GAP_M:
                    geo_total += d
            prev_xyz = (x, z)
        else:
            prev_xyz = None
        geo_vals.append(geo_total)
        g = f.get("g_force") or [0.0, 0.0, 0.0]
        pts.append({
            "dist": dist, "t": t, "t_rel": 0.0, "v": v,
            "speed_kph": f.get("speed_kph", 0.0),
            "throttle": f.get("throttle", 0.0),
            "brake": f.get("brake", 0.0),
            "glong": g[0] if len(g) > 0 else 0.0,
            # 横向 G（g_force[1]）。g_force 的分量顺序是 **纵向在前**：
            # 记录器写的是 [a_long/9.81, a_lat/9.81, 0]。
            # 有符号，用来判左右弯（正 = 左转，已用「外侧轮转得更快」验证过）。
            "glat": g[1] if len(g) > 1 else 0.0,
            # 合成 G 大小（横向 + 纵向）：与记录器写赛道轨迹点的算法一致，
            # 用来给「按 G 力着色」的赛车线上色。
            "gmag": round(math.hypot(g[0] if len(g) > 0 else 0.0,
                                     g[1] if len(g) > 1 else 0.0), 3),
            "x": f.get("car_x"), "z": f.get("car_z"),
        })
        prev = {"t": t, "v": v}

    if geo is True:
        use_geo = geo_total > 0
    elif geo is False:
        use_geo = False
    else:
        use_geo = bool(dist > 0 and geo_total > 0
                       and 0.5 <= geo_total / dist <= 2.0)
    if pts:
        t0 = pts[0]["t"]
        for i, p in enumerate(pts):
            p["t_rel"] = round(p["t"] - t0, 3)
            p["dist"] = round(geo_vals[i] if use_geo else p["dist"], 1)
    return pts


# —— 插值与重采样 ——————————————————————————————

def _interp(dists: list[float], values: list[float], d: float) -> float:
    if d <= dists[0]:
        return values[0]
    if d >= dists[-1]:
        return values[-1]
    i = bisect.bisect_right(dists, d) - 1
    d0, d1 = dists[i], dists[i + 1]
    if d1 <= d0:
        return values[i + 1]
    k = (d - d0) / (d1 - d0)
    return values[i] + (values[i + 1] - values[i]) * k


def resample_series(pts: list[dict], key: str, step: float = 10.0,
                    max_dist: float | None = None) -> tuple[list[float], list[float]]:
    """把 pts 里的 (dist, key) 序列重采样到固定距离网格。

    返回 (grid, values)。max_dist 缺省时取该序列的最远距离。
    """
    dists = [p["dist"] for p in pts]
    vals = [p.get(key, 0.0) for p in pts]
    if not dists:
        return [], []
    limit = max_dist if max_dist is not None else dists[-1]
    grid: list[float] = []
    d = 0.0
    while d < limit:
        grid.append(round(d, 1))
        d += step
    return grid, [_interp(dists, vals, d) for d in grid]


# —— 圈间时间差 ——————————————————————————————

def time_diff(cur_pts: list[dict], ref_pts: list[dict],
              step: float = 10.0) -> dict:
    """本圈 vs 参考圈的逐距离时间差。

    diff_ms = t_本圈(d) - t_参考圈(d)。**正值 = 本圈在该距离段丢时间**，
    负值 = 本圈更快。公共距离取两圈较短者。

    ref_t_rel_ms = 参考圈跑到该距离用的毫秒数——前端 X 轴同时标注
    「位置(米)」和「参考圈到此处的时间」，用户才知道正负发生在哪。
    """
    if not cur_pts or not ref_pts:
        return {"step": step, "grid": [], "diff_ms": [], "ref_t_rel_ms": []}
    common = min(cur_pts[-1]["dist"], ref_pts[-1]["dist"])
    grid: list[float] = []
    d = 0.0
    while d < common:
        grid.append(round(d, 1))
        d += step
    cur_d = [p["dist"] for p in cur_pts]
    cur_t = [p["t_rel"] for p in cur_pts]
    ref_d = [p["dist"] for p in ref_pts]
    ref_t = [p["t_rel"] for p in ref_pts]
    diff = [round((_interp(cur_d, cur_t, d) - _interp(ref_d, ref_t, d)) * 1000, 1)
            for d in grid]
    ref_ms = [round(_interp(ref_d, ref_t, d) * 1000, 1) for d in grid]
    return {"step": step, "grid": grid, "diff_ms": diff, "ref_t_rel_ms": ref_ms}


# —— 峰谷检测 ——————————————————————————————

def _smooth(values: list[float], window: int = 7) -> list[float]:
    if window < 3:
        return list(values)
    half = window // 2
    out = []
    n = len(values)
    for i in range(n):
        a = max(0, i - half)
        b = min(n, i + half + 1)
        seg = values[a:b]
        out.append(sum(seg) / len(seg))
    return out


def find_peaks_valleys(pts: list[dict], window: int = 7,
                       prominence_kph: float = 12.0) -> list[dict]:
    """平滑后找速度局部峰/谷，相邻极值差不足 prominence 的丢弃。

    返回 [{"distance": 米, "speed_kph": 值, "kind": "peak"|"valley"}, ...]
    （按距离升序，peak/valley 交替）
    """
    if len(pts) < window * 2:
        return []
    dists = [p["dist"] for p in pts]
    speeds = _smooth([p["speed_kph"] for p in pts], window)
    extrema: list[tuple[int, str]] = []
    for i in range(1, len(speeds) - 1):
        if speeds[i] >= speeds[i - 1] and speeds[i] > speeds[i + 1]:
            kind = "peak"
        elif speeds[i] <= speeds[i - 1] and speeds[i] < speeds[i + 1]:
            kind = "valley"
        else:
            continue
        if extrema and extrema[-1][1] == kind:
            # 同类相邻：保留更极值的一个
            j = extrema[-1][0]
            if (kind == "peak" and speeds[i] >= speeds[j]) or \
               (kind == "valley" and speeds[i] <= speeds[j]):
                extrema[-1] = (i, kind)
            continue
        extrema.append((i, kind))
    # prominence 过滤：相邻极值差 >= 阈值
    out: list[dict] = []
    for idx, (i, kind) in enumerate(extrema):
        if idx > 0:
            j = extrema[idx - 1][0]
            if abs(speeds[i] - speeds[j]) < prominence_kph:
                continue
        out.append({"distance": round(dists[i], 1),
                    "speed_kph": round(speeds[i], 1), "kind": kind})
    # 再次交替过滤
    final: list[dict] = []
    for x in out:
        if final and final[-1]["kind"] == x["kind"]:
            if (x["kind"] == "peak" and x["speed_kph"] > final[-1]["speed_kph"]) or \
               (x["kind"] == "valley" and x["speed_kph"] < final[-1]["speed_kph"]):
                final[-1] = x
            continue
        final.append(x)
    return final


# —— 踏板渐变赛车线 ————————————————————————————

def _clamp01(v) -> float:
    """把踏板开度规整到 0~1（顺手兼容误传成 0~100 百分数的情况）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    if x > 1.5:                     # 看起来是百分数（0~100）
        x /= 100.0
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _smooth3(xs: list[float]) -> list[float]:
    """三点滑动平均（首尾取自身），削弱逐点抖动导致的分段碎裂。"""
    n = len(xs)
    if n < 3:
        return xs
    return [(xs[max(0, i - 1)] + xs[i] + xs[min(n - 1, i + 1)]) / 3.0
            for i in range(n)]


def race_line(pts: list[dict], decimate: int = 6, eps: float = 0.04) -> dict:
    """参考圈赛车线：同一批点，**两套着色口径都带出来**。

    · 踏板口径（b / t）：刹车越重越红（粉→红），油门越深越绿（青→绿），都不踩 = 滑行。
    · G 力口径（g）：合成 G 大小（横向+纵向），与实时仪表盘的「按 G 力着色」同一算法。

    返回 {"segments": [{"color": "brake|throttle|coast",
                        "pts": [[x, z], ...],
                        "b": [刹车开度 0~1, ...],   # 与 pts 一一对齐
                        "t": [油门开度 0~1, ...],
                        "g": [合成 G 大小, ...]}]}

    🔴 两个容易踩的坑（都实测踩过）：
    1. **抽稀必须在分段之前对整条序列做**。之前是先分色、再在
       dashboard 里对每段各自 `pts[::6]`——每段末尾到下一个边界的
       点被丢掉，一圈上千个分色段就留下上千个肉眼可见的断隔，
       「明明跑完一整圈，线却是斑驳的」。
    2. **相邻两段必须共享边界点**（新段从上一段的末点接起），
       否则换色处那一帧的弦谁都不画（canvas 分多次 stroke 时
       段与段的衔接靠端点重合，不重合就是缺口）。
    另外对踏板值做三点平滑：逐点抖动会把一条线打成上千个小段。

    ⚠️ 分段是按「踏板通道」切的；切到 G 力着色时前端会把同一条线整条重上色，
       分段本身不影响 G 模式的观感（只是多几次 stroke）。
    """
    sub = pts[::decimate] if decimate and decimate > 1 else pts
    bs = _smooth3([_clamp01(p.get("brake")) for p in sub])
    ts = _smooth3([_clamp01(p.get("throttle")) for p in sub])

    def channel(b: float, t: float) -> str:
        # 两踏板同时踩时以刹车为准（trail braking 视觉上按刹车画）
        if b >= t and b > eps:
            return "brake"
        if t > eps:
            return "throttle"
        return "coast"

    segments: list[dict] = []
    cur_ch, cur_pts, cur_b, cur_t, cur_g = None, [], [], [], []
    for i, p in enumerate(sub):
        c = channel(bs[i], ts[i])
        if c != cur_ch:
            if cur_pts:
                segments.append({"color": cur_ch, "pts": cur_pts,
                                 "b": cur_b, "t": cur_t, "g": cur_g})
                # 从上一段末点接起，保证换色处首尾相接（含各通道值）
                cur_pts = [cur_pts[-1]]
                cur_b, cur_t, cur_g = [cur_b[-1]], [cur_t[-1]], [cur_g[-1]]
            else:
                cur_pts, cur_b, cur_t, cur_g = [], [], [], []
            cur_ch = c
        cur_pts.append([p.get("x"), p.get("z")])
        cur_b.append(round(bs[i], 3))
        cur_t.append(round(ts[i], 3))
        cur_g.append(round(p.get("gmag") or 0.0, 3))
    if cur_pts:
        segments.append({"color": cur_ch, "pts": cur_pts,
                         "b": cur_b, "t": cur_t, "g": cur_g})
    return {"segments": segments}


def race_line_of_lap(frames: list[dict] | None = None, lap_no: int = 0,
                     decimate: int = 6,
                     grouped: dict[int, list[dict]] | None = None) -> dict:
    """指定圈的赛车线（给「行车轨迹」卡片单独切圈用，不必重算整页分析）。

    圈口径复用 clean_laps，与详情页其它分析同一套，避免圈号对不上。

    `grouped` 可选：调用方已有 clean_laps 的结果就直接传进来（`frames` 可不传）。
    切圈是交互动作，每次都拿 21 万帧重跑一遍 clean_laps 太亏。
    """
    laps = clean_laps(frames) if grouped is None else grouped
    fs = laps.get(lap_no)
    if not fs or len(fs) < 2:
        return {"lap": lap_no, "segments": []}
    return {"lap": lap_no, "segments": race_line(lap_samples(fs),
                                                 decimate=decimate)["segments"]}


# —— 分段计时 / 理论最快圈 ——————————————————————

def _lap_sector_split(fs: list[dict], n_sectors: int):
    """一圈按**距离**等分 → (各段用时, 圈长m, 圈总用时s)；采样点不足返回 None。

    用距离等分而不是时间等分：段边界固定在同一段路上，跨圈才有可比性
    （时间等分会让每圈的"段"落在不同位置，段用时差就失去意义）。
    """
    pts = lap_samples(fs)
    if len(pts) < 10:
        return None
    total = pts[-1]["dist"]
    if total <= 0:
        return None
    dists = [q["dist"] for q in pts]
    trels = [q["t_rel"] for q in pts]
    times = []
    for k in range(n_sectors):
        a = _interp(dists, trels, total * k / n_sectors)
        b = _interp(dists, trels, total * (k + 1) / n_sectors)
        times.append(b - a)
    return times, total, pts[-1]["t_rel"]


def sector_times(laps: dict[int, list[dict]], n_sectors: int = 4,
                 tol: float = 0.05, dist_tol: float = 0.03,
                 official: dict[int, float] | None = None) -> dict:
    """分段计时 + 理论最快圈。

    每圈按距离等分成 n_sectors 段、插值出各段用时；每段取所有**可信圈**里
    的最快值求和 = 理论最快圈。它与实际最快圈的差，就是「你已经有能力跑出来、
    只是还没在同一圈里连起来」的时间 —— 这才是分段计时真正有用的产出。

    🔴 必须先过滤异常圈：冲出赛道 / 进站 / 打转的圈里，某一小段可能
       "恰好很快"，把理论值拉到不真实地低。实测 LM55 23 圈不过滤得到
       +8.48% 的假空间，只留 ≤最快圈*(1+tol) 的 5 圈才是真实的 +0.71%。
       所以理论值**只在可信圈 ≥2 圈时**才给出，否则宁可空着（reliable=False）
       也不给一个会骗人的数字。

    🔴 圈长不一致的圈必须单独排除（dist_tol 那一路）。段边界是按各圈**自己的**
       圈长等分的，所以只有"跑满一整圈"的圈，段边界才落在同一段路上。实测同一场
       里第 1 圈只有 6129.6m（最快圈 6937.8m 的 88%，录像从半圈处开始），
       它的 S1 插出来 25.229s，比全场最快的 33.208s 还"快" 8 秒 —— 纯粹是
       边界错位，不是真本事。
       ⚠️ 这不只是显示难看：**圈长偏短的圈总时长也偏短，完全可能被选成
       actual_best**，那样"实际最快圈"和"潜在空间"一起就错了。所以筛选
       发生在选 actual_best **之前**，不只是从 counted 里剔掉。
       判据用基准圈长 ±dist_tol（`_reference_lap_dist`，最大同长簇；圈数 <3
       时不按距离筛）：出赛道多跑一截会让圈长变长，取最大反而会以偏为准。

    🔴 `actual_best_lap` 优先用官方 `last_lap`（与圈速表/圈间对比同一口径），
       段用时按 官方/span 等比例缩放，保证各段之和等于展示的圈速。

    ⚠️ 理论最快圈 < 实际最快圈是**正常**的，不要把它"修"成相等。
    ⚠️ 残余偏差：即使同为完整圈，圈长仍有约 ±0.3% 的差（本次 5 个可信圈
       6936~6960m），段边界会错开十几米，理论值因此可能乐观 0.1~0.3s。
       所以 laps[].dist_m 要展示出来，让人看得见可比性。
    """
    out: dict = {
        "n_sectors": n_sectors, "tol": tol, "dist_tol": dist_tol,
        "laps": [], "best_each_s": [],
        "theoretical_best_s": None, "actual_best_s": None,
        "actual_best_lap": None,
        "potential_gain_s": None, "potential_gain_pct": None,
        "counted_laps": [], "partial_laps": [], "ref_dist_m": None,
        "reliable": False, "note": "",
    }
    raw: dict[int, tuple] = {}
    for ln, fs in sorted(laps.items()):
        r = _lap_sector_split(fs, n_sectors)
        if r is not None:
            raw[ln] = r
    if not raw:
        out["note"] = "没有可用于分段的圈（采样点不足）"
        return out

    # —— 圈长一致性：只有跑满整圈的圈，段边界才落在同一段路上 ——
    # 🔴 基准优先用 _reference_lap_dist（最大同长簇）。1~2 圈时它不可用
    #    （返回 0），退回「上中位数」——与旧行为一致。
    # 🔴 先排掉「时钟跳变/多场合并」的圈：它们的 span 远大于官方用时
    #    （>1.5x），距离被放大到 3 倍，会把基准拉歪（实测 223244：2 圈，
    #    中位数取到 21855m 的假圈，真实圈被判 partial、最快圈报错）。
    if official is None:
        official = official_lap_times(laps)
    normal_dists = {n: v[1] for n, v in raw.items()
                    if not (official.get(n) and v[2] > official[n] * 1.5)}
    d_ref = _reference_lap_dist(normal_dists) if normal_dists else 0.0
    if d_ref <= 0:
        if normal_dists:
            vals = sorted(normal_dists.values())
            d_ref = vals[len(vals) // 2]       # 上中位数（1~2 圈）
        else:
            d_ref = min(v[1] for v in raw.values())   # 全被跳变排除 → 取最短
    out["ref_dist_m"] = round(d_ref, 1)
    usable = {n: v for n, v in raw.items()
              if abs(v[1] - d_ref) <= d_ref * dist_tol}
    out["partial_laps"] = sorted(set(raw) - set(usable))
    if not usable:
        # 圈长全都对不上（数据异常），退回全量但明确标注不可信
        usable = dict(raw)
        out["note"] = "所有圈的圈长都偏离基准，分段不可比"

    # 圈速优先用官方 last_lap：帧跨度会把暂停/进站算进去，选出的
    # 「实际最快圈」会与圈速表/圈间对比不一致。
    def _lap_total(n: int) -> float:
        off = official.get(n)
        return off if off is not None else raw[n][2]

    actual_best_lap = min(usable, key=_lap_total)
    actual_best = _lap_total(actual_best_lap)
    counted = {n: v for n, v in usable.items()
               if _lap_total(n) <= actual_best * (1 + tol)}
    out["actual_best_lap"] = actual_best_lap
    out["actual_best_s"] = round(actual_best, 3)

    # 段用时按官方圈速等比例缩放：暂停/进站让 span 大于官方值，
    # 不缩放的话「各段之和」与 total_s 对不上，理论最快圈会算出负数空间。
    scaled: dict[int, list[float]] = {}
    for ln, (times, _dist, span_s) in raw.items():
        off = official.get(ln)
        if off is not None and span_s > 0 and abs(off - span_s) > 1e-6:
            k = off / span_s
            scaled[ln] = [x * k for x in times]
        else:
            scaled[ln] = times

    if len(counted) >= 2:
        best_each = [min(scaled[n][k] for n in counted)
                     for k in range(n_sectors)]
        theo = sum(best_each)
        out["reliable"] = True
        out["counted_laps"] = sorted(counted)
        out["best_each_s"] = [round(x, 3) for x in best_each]
        out["theoretical_best_s"] = round(theo, 3)
        out["potential_gain_s"] = round(actual_best - theo, 3)
        out["potential_gain_pct"] = round((actual_best - theo) / actual_best * 100, 2)
    else:
        # 只统计了信息性的段最快（不作为结论呈现）
        out["best_each_s"] = [
            round(min(scaled[n][k] for n in usable), 3) for k in range(n_sectors)]
        out["note"] = (f"接近最快圈（≤{actual_best * (1 + tol):.3f}s）的圈只有 "
                       f"{len(counted)} 个，不足 2 个，理论最快圈不可信")

    for ln in sorted(raw):
        _times, dist, span_s = raw[ln]
        off = official.get(ln)
        total_s = off if off is not None else span_s
        times = scaled[ln]
        partial = ln not in usable
        row = {
            "lap": ln, "total_s": round(total_s, 3),
            "span_s": round(span_s, 3),
            "time_source": "official" if off is not None else "span",
            "dist_m": round(dist, 1),
            "sectors": [round(x, 3) for x in times],
            "counted": ln in counted, "is_best": ln == actual_best_lap,
            "partial": partial,
        }
        # 残缺圈的段边界不在同一段路上，给差值只会误导，干脆不给
        if out["reliable"] and not partial:
            be = out["best_each_s"]
            row["deltas"] = [round(times[k] - be[k], 3) for k in range(n_sectors)]
        out["laps"].append(row)
    return out


# —— 轮胎滑移（空转 / 抱死）——————————————————————
#
# 只有 wheel_rads（四轮角速度）是「车轮实际转多快」的来源 —— 车身速度传感器
# 测不出空转和抱死。滑移率 s = (ω·R − v) / v：
#   s > 0 轮子转得比车快（空转 / 打滑）；s < 0 轮子转得比车慢（抱死 / 拖胎）。

# 自由滚动判据（标定只用这些帧）。只有自由滚动时 ω·R ≈ v 才成立。
_SLIP_FREE_THR = 0.05          # 油门开度
_SLIP_FREE_BRK = 0.05          # 刹车开度
_SLIP_FREE_V_MIN = 10.0        # m/s（36 km/h）—— 低速时 ω 与 v 的信噪比都差
_SLIP_FREE_G = 0.3             # 纵向与横向加速度都要小（单位 g）

# 事件阈值。取实测噪声底之上很远：自由滚动帧 |s| ≈ 0.0005，
# 而空转峰值 +16%、抱死最低 −96%，信噪比约 100:1，所以阈值不敏感。
_SLIP_SPIN_THR = 0.10          # 后轴比车快 10% → 空转
_SLIP_LOCK_THR = -0.15         # 前轴比车慢 15% → 抱死
_SLIP_MIN_V = 3.0              # 统计滑移的最小速度（m/s），防低速除出噪声


def _slip_percentiles(xs: list[float]) -> dict:
    if not xs:
        return {}
    s = sorted(xs)
    n = len(s)

    def q(p: float) -> float:
        return round(s[min(n - 1, max(0, int(n * p)))], 3)
    return {"min": round(s[0], 3), "p05": q(0.05), "p50": q(0.50),
            "p95": q(0.95), "max": round(s[-1], 3)}


def _wheel_slip_rows(laps: dict[int, list[dict]]):
    """全部帧 → (per_lap 行, 自由滚动帧, 总帧数)。

    行格式 (t, v, ω前轴, ω后轴, throttle, brake, |glon|, |glat|)。
    只被 wheel_slip 与 calibrate_wheel_radii 使用——两处共用一份行构造，
    别再各写一份。
    """
    per_lap: dict[int, list[tuple]] = {}
    for ln, fs in sorted(laps.items()):
        buf: list[tuple] = []
        for f in fs:
            r = f.get("wheel_rads")
            if not r or len(r) < 4:
                continue
            v = (f.get("speed_kph") or 0.0) / 3.6
            g = f.get("g_force") or (0.0, 0.0, 0.0)
            glon = abs(g[0]) if len(g) > 0 else 0.0
            glat = abs(g[1]) if len(g) > 1 else 0.0
            buf.append((f.get("t") or 0.0, v, (r[0] + r[1]) / 2.0,
                        (r[2] + r[3]) / 2.0, f.get("throttle") or 0.0,
                        f.get("brake") or 0.0, glon, glat))
        if buf:
            per_lap[ln] = buf
    free = [r for rows in per_lap.values() for r in rows
            if r[4] < _SLIP_FREE_THR and r[5] < _SLIP_FREE_BRK
            and r[1] > _SLIP_FREE_V_MIN and r[6] < _SLIP_FREE_G
            and r[7] < _SLIP_FREE_G]
    total_rows = sum(len(v) for v in per_lap.values())
    return per_lap, free, total_rows


def _calibrate_radii_from_free(free: list, total_rows: int):
    """自由滚动帧上最小二乘标定 → (rf, rr, calib 展示字典)。

    🔴 rf/rr 是**未取整**的原始值，计算滑移必须用它们；calib 里带 round(4)
       的展示值。自由滚动帧不足时 rf/rr 返回 0，calib.reason 写明原因、
       ok=False——调用方据此放弃（不产出滑移结论）。
    """
    def _calib(pick) -> float:
        num = sum(r[1] * pick(r) for r in free)
        den = sum(pick(r) ** 2 for r in free)
        return num / den if den > 0 else 0.0

    rf, rr = _calib(lambda r: r[2]), _calib(lambda r: r[3])
    calib: dict = {
        "front_m": round(rf, 4), "rear_m": round(rr, 4),
        "ratio": round(rf / rr, 4) if rr else None,
        "free_frames": len(free), "total_frames": total_rows,
        "free_pct": round(len(free) / total_rows * 100, 1) if total_rows else 0.0,
        "ok": False, "reason": "",
    }
    if len(free) < 50 or rf <= 0 or rr <= 0:
        calib["reason"] = (f"自由滚动帧只有 {len(free)} 帧，标定不可靠"
                          f"（需要 ≥50 帧）")
        return rf, rr, calib

    def s_front(r) -> float:
        return (r[2] * rf - r[1]) / r[1] if r[1] > 0 else 0.0

    def s_rear(r) -> float:
        return (r[3] * rr - r[1]) / r[1] if r[1] > 0 else 0.0

    # 自洽性：标定用的自由滚动帧上滑移应≈0
    f_mean = sum(s_front(r) for r in free) / len(free)
    r_mean = sum(s_rear(r) for r in free) / len(free)
    calib["free_slip_front"] = round(f_mean, 4)
    calib["free_slip_rear"] = round(r_mean, 4)
    calib["ok"] = abs(f_mean) < 0.02 and abs(r_mean) < 0.02
    if not calib["ok"]:
        calib["reason"] = (f"自由滚动帧滑移均值偏离 0 过多"
                           f"（前 {f_mean:+.4f} / 后 {r_mean:+.4f}），标定可能被污染")
    return rf, rr, calib


def calibrate_wheel_radii(laps: dict[int, list[dict]]) -> dict:
    """公共入口：自由滚动帧上自标定前后轴轮胎半径。

    与 /slip（wheel_slip）共用同一套行构造与标定逻辑（单一事实源）；
    事件检测器的适配层用它拿到标定半径，别在第二处再写一份最小二乘。

    返回 {available, front_m, rear_m(未取整), ratio, free_frames, ok, reason,
          free_slip_front, free_slip_rear}。available=False 时不要用半径。
    """
    per_lap, free, total_rows = _wheel_slip_rows(laps)
    if not per_lap:
        return {"available": False,
                "reason": "本场次没有 wheel_rads（四轮角速度）字段"}
    rf, rr, calib = _calibrate_radii_from_free(free, total_rows)
    usable = len(free) >= 50 and rf > 0 and rr > 0
    out = dict(calib)
    out["available"] = usable
    out["front_m"] = rf        # 未取整：滑移计算用
    out["rear_m"] = rr
    return out


def wheel_slip(laps: dict[int, list[dict]], nmax_events: int = 5,
               max_per_lap: int = 120) -> dict:
    """用四轮角速度检测空转与抱死。

    🔴 半径是**自标定**的，不依赖任何外部参数：自由滚动帧上 ω·R ≈ v，
       于是 R = Σ(v·ω) / Σ(ω²)（对 R 的最小二乘解，即 argmin Σ(v−ωR)² 的解）。

    🔴 前后轴必须**分别**标定：GT7 前后胎规格常不同，实测 R前 0.3395 m /
       R后 0.3440 m（比值 0.987）。若假设同半径，滑移率会被整体偏置约 1.3%，
       而抱死的均值信号本身只有 −7.8%，偏置不可忽略。

    自洽性检查：标定后自由滚动帧的滑移均值应接近 0。偏得远说明标定被污染
    （自由滚动帧太少，或油门/刹车阈值没生效），此时 calibration.ok 为 False。

    缺 wheel_rads 的场次返回 available=False —— 老场次（含合成的测试数据）
    没有这个字段，前端据此整卡隐藏，不要显示空表。

    抽稀按「每圈目标点数」而不是固定步长：固定步长下长圈点密、短圈点疏，
    而且总输出随帧数线性膨胀（本场 217k 帧、23 圈，步长 4 会产出 54k 点、
    约 2.5 MB JSON）。改成每圈 ≤max_per_lap 点后，输出被圈数封顶
    （23×120 ≈ 2.8k 点 / 约 120 KB），且各圈曲线密度一致、可比。

    ⚠️ 抽稀会漏掉 1~2 帧的尖峰（抱死常常就这么短）。所以图表负责趋势、
       events 负责峰值的分工不能倒过来 —— 尖峰必须从 events 读，别从曲线读。
    """
    per_lap, free, total_rows = _wheel_slip_rows(laps)
    if not per_lap:
        return {"available": False,
                "reason": "本场次没有 wheel_rads（四轮角速度）字段"}

    # —— 1. 自由滚动帧上自标定前后轴半径（公共实现，见 calibrate_wheel_radii）——
    rf, rr, calib = _calibrate_radii_from_free(free, total_rows)
    if len(free) < 50 or rf <= 0 or rr <= 0:
        return {"available": False, "reason": calib["reason"],
                "calibration": calib}

    def s_front(r) -> float:
        return (r[2] * rf - r[1]) / r[1] if r[1] > 0 else 0.0

    def s_rear(r) -> float:
        return (r[3] * rr - r[1]) / r[1] if r[1] > 0 else 0.0

    # （自洽性检查在 _calibrate_radii_from_free 里已完成，calib 已带
    #   free_slip_front/rear 与 ok——这里别再算第二遍。）

    # —— 2. 逐圈统计 + 事件（连续帧聚成一次，否则一次抱死会被算成 60 次）——
    lap_rows: list[dict] = []
    lock_events: list[dict] = []
    spin_events: list[dict] = []
    series: dict[int, dict] = {}

    def _event(ln: int, t0: float, worst: tuple, getter, nframes: int) -> dict:
        return {
            "lap": ln, "t_rel": round(worst[0] - t0, 3),
            "slip": round(getter(worst), 3),
            "speed_kph": round(worst[1] * 3.6, 1),
            "throttle": round(worst[4] * 100, 0),
            "brake": round(worst[5] * 100, 0),
            "frames": nframes,
        }

    for ln, rows in per_lap.items():
        sf: list[float] = []
        sr: list[float] = []
        lock_run: list[tuple] = []
        spin_run: list[tuple] = []
        dec = {"t": [], "front": [], "rear": [], "speed_kph": [],
               "throttle": [], "brake": []}
        t0 = rows[0][0]
        stride = max(1, math.ceil(len(rows) / max(1, max_per_lap)))

        def _flush_lock(run: list[tuple]) -> None:
            """一次抱死结束：取这一段里**最负**的前轴滑移作为严重度。"""
            if len(run) >= 2:      # 单帧尖峰不算一次事件
                w = min(run, key=s_front)
                lock_events.append(_event(ln, t0, w, s_front, len(run)))

        def _flush_spin(run: list[tuple]) -> None:
            """一次空转结束：取这一段里**最正**的后轴滑移作为严重度。"""
            if len(run) >= 2:
                w = max(run, key=s_rear)
                spin_events.append(_event(ln, t0, w, s_rear, len(run)))

        for i, r in enumerate(rows):
            f_ = s_front(r)
            r_ = s_rear(r)
            if r[1] > _SLIP_MIN_V:
                sf.append(f_)
                sr.append(r_)
                if r[5] > 0.85 and f_ < _SLIP_LOCK_THR:
                    lock_run.append(r)
                else:
                    _flush_lock(lock_run)
                    lock_run = []
                if r[4] > 0.95 and r_ > _SLIP_SPIN_THR:
                    spin_run.append(r)
                else:
                    _flush_spin(spin_run)
                    spin_run = []
            if i % stride == 0:
                dec["t"].append(round(r[0] - t0, 2))
                dec["front"].append(round(f_, 3))
                dec["rear"].append(round(r_, 3))
                dec["speed_kph"].append(round(r[1] * 3.6, 1))
                dec["throttle"].append(round(r[4] * 100, 0))
                dec["brake"].append(round(r[5] * 100, 0))
        _flush_lock(lock_run)
        _flush_spin(spin_run)
        series[ln] = dec

        lock_frames = sum(1 for r in rows if r[1] > _SLIP_MIN_V
                          and r[5] > 0.85 and s_front(r) < _SLIP_LOCK_THR)
        spin_frames = sum(1 for r in rows if r[1] > _SLIP_MIN_V
                          and r[4] > 0.95 and s_rear(r) > _SLIP_SPIN_THR)
        lap_rows.append({
            "lap": ln, "frames": len(rows),
            "front": _slip_percentiles(sf), "rear": _slip_percentiles(sr),
            "lockup_frames": lock_frames, "wheelspin_frames": spin_frames,
        })

    lock_events.sort(key=lambda e: e["slip"])
    spin_events.sort(key=lambda e: e["slip"], reverse=True)
    return {
        "available": True,
        "max_per_lap": max_per_lap,
        "calibration": calib,
        "laps": lap_rows,
        "lockup": {"events": len(lock_events),
                   "frames": sum(r["lockup_frames"] for r in lap_rows),
                   "worst": lock_events[:nmax_events]},
        "wheelspin": {"events": len(spin_events),
                      "frames": sum(r["wheelspin_frames"] for r in lap_rows),
                      "worst": spin_events[:nmax_events]},
        "series": series,
    }


# —— 走线偏差（本圈相对参考圈的横向偏移） ————————————

_DEV_STEP = 5.0        # 输出网格步长（米）
_DEV_WIN = 24          # 最近点单调搜索窗口（以参考线采样点数为单位，见下）
_DEV_COVER = 0.6       # 本圈至少要覆盖参考线这么多比例才算可信
_DEV_START_GAP = 60.0  # 本圈起点与参考圈起点的物理间距超过这个值 ⇒ 不同一位置起步
_DEV_RMS_TOL = 60.0    # RMS 偏移超过这个值 ⇒ 对齐明确失败，绝不当结论输出


def _ref_self_cross(R: list, ref_len: float, min_sep: float = 250.0) -> float:
    """参考线上「沿赛道相隔 > min_sep 米、空间上最近」的那对点的间距。

    单圈赛道这个值通常是几十米往上（平行直道之间、回头弯的内侧）。
    撞车/出赛道导致**折返**的圈会出现很小的值（轨迹自己压到自己），
    此时最近点匹配有歧义、单调搜索会锁死在错的分支上。

    🔴 **探针实测发现这个指标单独使用不可靠**：
      · 有些赛道两段本来就只差十几米（平行直道、内嵌的回头弯），
        用 8~25 m 阈值会误杀干净圈；
      · 有些「不那么脏」的圈（出赛道但没折返）自交指标仍是 ∞。
      所以这里**只做诊断输出**（`ref_self_cross_m`），不作为硬护栏——
      实际失败会被 RMS 护栏挡住。这种「诊断够用、护栏难定」的指标
      老实标出来就行，别硬上阈值。网格分桶实现，O(n)。
    """
    cell = 8.0
    buckets: dict[tuple[int, int], list[int]] = {}
    best = float("inf")
    for i, (s, x, z) in enumerate(R):
        cx, cz = int(x // cell), int(z // cell)
        for gx in (cx - 1, cx, cx + 1):
            for gz in (cz - 1, cz, cz + 1):
                for j in buckets.get((gx, gz), ()):
                    ds = abs(s - R[j][0])
                    ds = min(ds, ref_len - ds)      # 闭环：首尾是同一个点
                    if ds < min_sep:
                        continue
                    d = math.hypot(R[j][1] - x, R[j][2] - z)
                    if d < best:
                        best = d
        buckets.setdefault((cx, cz), []).append(i)
    return best


def _seg_closest(ax: float, az: float, bx: float, bz: float,
                 px: float, pz: float):
    """点 P 到线段 AB 的最近点。

    返回 (t, d2, dx, dz)：t 为投影比例（夹到 [0,1]），d2 为平方距离，
    (dx, dz) 为线段方向向量（未归一化，供算法向用）。
    """
    dx, dz = bx - ax, bz - az
    L2 = dx * dx + dz * dz
    if L2 <= 0.0:
        return 0.0, (px - ax) ** 2 + (pz - az) ** 2, 0.0, 0.0
    t = ((px - ax) * dx + (pz - az) * dz) / L2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    fx, fz = ax + dx * t, az + dz * t
    return t, (px - fx) ** 2 + (pz - fz) ** 2, dx, dz


def track_deviation(ref_pts: list[dict], cur_pts: list[dict],
                    step: float = _DEV_STEP,
                    offtrack_m: float = 5.0) -> dict:
    """本圈相对参考圈的**横向偏移**（米，带符号）——「走线偏差」。

    做法（几何对齐，不是按距离对齐）：
      1. 把参考圈的点列当成折线。
      2. 本圈每个点投影到折线上找**最近点**，得到三个量：
         沿参考线的弧长 s、横向偏移 dlat、以及参考线在该处的切向。
      3. dlat = (本圈点 − 垂足) 点乘参考线法向，所以**带符号**。
      4. 把 (s, dlat) 重采样到固定距离网格输出。

    🔴 **为什么不能用「按距离对齐」**——本项目已有的其它分析（时间差、峰谷配对）
       是按各圈自己积分出的累计距离对齐的，而 `match_pv_pairs` 里实测记录了
       「同场两圈同一弯道的积分漂移可达 60~300 m」（丢包时 `lap_samples` 会跳过
       整段距离，见那个 dt<=1.0 的守卫）。
       横向偏移对沿赛道错位**极其**敏感：100 m 的错位在半径 200 m 的弯里就是
       约 6 m 的假偏移，而真实走线差才 1~5 m —— 等于把噪声当信号。
       几何对齐天然免疫：垂足在参考线上，沿赛道误差恒为 0，剩下的
       dlat 才是真的横向差。

    🔴 符号约定：法向取参考线切向的 **+90° 旋转**，即 N = (−Tz, Tx)。
       所以「正」= 参考线行进方向的左侧。但**别依赖它去说左右**——
       世界坐标的左右手性容易搞反。要判内侧/外侧请用 `inside`
       （由参考线自身的曲率方向算出，不依赖坐标系手性）。

    🔴 只有**跑满整圈**的本圈才可信：残圈只覆盖参考线的一小段，
       覆盖率（coverage）会远低于 1，此时可信度直接标 False。
       判据与 sector_times 的残缺圈过滤同源。

    返回：
      {step_m, ref_len_m, cur_len_m, coverage, ref_lap_cover, reliable,
       rms_dlat, p95_abs_dlat, max_abs_dlat, mean_dlat, inside_pct,
       worst_win: {from_m, to_m, mean_dlat}, segments: [...],
       line: [[x, z, dlat, inside], ...],   # 本圈走线，沿参考线弧长等距
       grid_m: [s, ...],                    # 与 line 一一对应的弧长
       drift_m}                             # 距离积分漂移诊断
       + offtrack_runs（|dlat|>offtrack_m 的出界段，见下）

    offtrack_runs：`[{"from_dist_m","to_dist_m","max_abs_dlat",
                      "from_s_m","to_s_m"}]`。dist 是**本圈自己的累计距离**
    （能映射回本圈时间），s 是参考线弧长（能定位在赛道哪里）。
    事件时间线的 OFF_TRACK 用它做几何判据——比「低速+高滑移」可靠：
    压草地未必滑移大，高速冲出弯也出界。最短 8 m 过滤噪声尖刺。

    🔴 `step` 必须先归一化再进重采样循环：`while d <= s_end: d += step`
       在 step<=0 时**永不推进**（负数还会反向远离），是一条能跑死整个进程的
       死循环。HTTP 入口已夹紧，但这是模块级公开函数，测试/重放/未来新调用方
       都会绕开那一层——所以守卫必须放在这里，不能只放在入口。
    """
    # 归一化步长：0 / 负数 / NaN / inf 一律回落到默认值，再夹进合理区间。
    try:
        step = float(step)
    except (TypeError, ValueError):
        step = _DEV_STEP
    if not math.isfinite(step) or step <= 0.0:
        step = _DEV_STEP
    step = max(0.5, min(step, 200.0))

    empty = {"step_m": step, "line": [], "grid_m": [], "segments": [],
             "reliable": False, "reason": "",
             "ref_len_m": 0.0, "cur_len_m": 0.0, "coverage": 0.0,
             "rms_dlat": 0.0, "p95_abs_dlat": 0.0, "max_abs_dlat": 0.0,
             "mean_dlat": 0.0, "inside_pct": None, "drift_m": 0.0,
             "start_arc_m": 0.0, "start_gap_m": 0.0, "end_gap_m": 0.0,
             "ref_self_cross_m": None, "worst_win": None,
             "offtrack_runs": []}
    if not ref_pts or not cur_pts or len(ref_pts) < 10 or len(cur_pts) < 10:
        empty["reason"] = "点太少"
        return empty

    # 参考折线：直接用原始采样点（50 Hz 下高速约 1.4 m 一个点，
    # 已经比任何重采样都准），并只保留坐标有效的点。
    R = [(p["dist"], p["x"], p["z"]) for p in ref_pts
         if p.get("x") is not None and p.get("z") is not None]
    if len(R) < 10:
        empty["reason"] = "参考圈没有有效坐标"
        return empty
    ref_len = R[-1][0]
    if ref_len <= 10.0:
        empty["reason"] = "参考圈长度异常"
        return empty

    # 单调搜索：本圈与参考圈同向推进，所以每个点只需在上一次匹配附近找。
    # 窗口 ±_DEV_WIN 个参考点（约 ±34 m 弧长）。这样既 O(n)，
    # 又不会在「赛道两段相距很近」的地方跳到另一支（沿赛道长度上它们
    # 差得远，窗口够不到）。
    rows = []            # (s, dlat, inside, x, z, cur_dist)
    j = 0
    for p in cur_pts:
        x, z = p.get("x"), p.get("z")
        if x is None or z is None:
            continue
        lo = j - _DEV_WIN
        if lo < 0:
            lo = 0
        hi = j + _DEV_WIN
        if hi > len(R) - 2:
            hi = len(R) - 2
        best = None
        for k in range(lo, hi + 1):
            t, d2, dx, dz = _seg_closest(R[k][1], R[k][2],
                                         R[k + 1][1], R[k + 1][2], x, z)
            if best is None or d2 < best[0]:
                best = (d2, k, t, dx, dz)
        if best is None:
            continue
        d2, k, t, dx, dz = best
        j = k
        s = R[k][0] + (R[k + 1][0] - R[k][0]) * t
        L = math.hypot(dx, dz)
        if L <= 0.0:
            continue
        # 法向 = 切向 +90° 旋转：(−dz, dx)/L
        nx, nz = -dz / L, dx / L
        fx, fz = R[k][1] + dx * t, R[k][2] + dz * t
        dlat = (x - fx) * nx + (z - fz) * nz
        # 曲率方向：T' = κN ⇒ cross(T, T') = κ(Tx² + Tz²) = κ，
        # 所以曲率中心就在 +N 一侧当且仅当 cross > 0。点在**弯心侧**
        # （=内侧）等价于 dlat 与 cross 同号。用前后两点估 cross 就够。
        k0 = max(0, k - 2)
        k1 = min(len(R) - 1, k + 2)
        t0x, t0z = R[k][1] - R[k0][1], R[k][2] - R[k0][2]
        t1x, t1z = R[k1][1] - R[k][1], R[k1][2] - R[k][2]
        cross = t0x * t1z - t0z * t1x
        c = cross
        # 贴着参考线（|dlat| 极小）时点就在线上，既不算内侧也不算外侧，
        # 否则「自己跟自己比」会算出 inside_pct = 0% 这种假结论。
        inside = None if abs(dlat) < 0.05 else ((dlat * c) > 0.0 if c != 0.0 else None)
        rows.append((s, dlat, inside, x, z, p.get("dist") or 0.0))

    if len(rows) < 10:
        empty["reason"] = "本圈可用点太少"
        return empty

    # 距离积分漂移诊断：同一物理位置处「本圈自己的累计距离」与
    # 「参考线弧长」的最大偏差。数值大说明该圈有丢包或空转/锁死，
    # 属于「结论之外但要如实交代」的量 —— 见 match_pv_pairs 的实测记录。
    drift = max(abs(r[5] - r[0]) for r in rows)
    start_arc = rows[0][0]
    end_arc = rows[-1][0]
    cover = end_arc / ref_len
    # 物理起终点间距：同赛道上车从同一位置过线，所以两者的起点应当几乎重合。
    # 出场圈（发车格起步，位置在起跑线之前）会让对应关系**跨圈回绕**，
    # 而参考折线是线性的、单调搜索接不住 —— 实测第 1 圈因此算出 524 m 的假
    # RMS。这个检查就是为它准备的（与赛道指纹依赖的是同一条前提）。
    start_gap = math.hypot(R[0][1] - rows[0][3], R[0][2] - rows[0][4])
    end_gap = math.hypot(R[-1][1] - rows[-1][3], R[-1][2] - rows[-1][4])

    vals = [abs(r[1]) for r in rows]
    svals = sorted(vals)
    n = len(svals)
    rms = math.sqrt(sum(v * v for v in vals) / n)
    self_cross = _ref_self_cross(R, ref_len)

    # 三道独立护栏，全都要过才给结论。宁可明确说「不可比」，
    # 也不能吐一个 500 m 的假横向偏移出去 —— 那比没有更糟。
    # （自交检测只做诊断，详见 _ref_self_cross 的注释。）
    reasons = []
    if start_gap > _DEV_START_GAP:
        reasons.append(
            f"本圈起点与参考圈起点相距 {start_gap:.0f} m，不是同一位置起步"
            "（出场圈 / 维修区起步），对应关系会跨圈回绕")
    if cover < _DEV_COVER:
        reasons.append(f"本圈只覆盖参考线的 {cover * 100:.0f}%")
    if rms > _DEV_RMS_TOL:
        reasons.append(f"偏移量级 {rms:.0f} m 明显不合理，对齐失败")
    reliable = not reasons

    # 重采样到等弧长网格
    line: list[list[float]] = []
    grid: list[float] = []
    s_end = end_arc
    d = 0.0
    idx = 0
    while d <= s_end:
        while idx + 1 < len(rows) and rows[idx + 1][0] < d:
            idx += 1
        r = rows[idx]
        line.append([round(r[3] if r[3] is not None else 0.0, 2),
                     round(r[4] if r[4] is not None else 0.0, 2),
                     round(r[1], 3),
                     1 if r[2] else (0 if r[2] is not None else -1)])
        grid.append(round(d, 1))
        d += step

    ins_known = [r[2] for r in rows if r[2] is not None]
    out = {
        "step_m": step,
        "ref_len_m": round(ref_len, 1),
        "cur_len_m": round(rows[-1][5], 1),
        "coverage": round(cover, 3),
        "reliable": reliable,
        "reason": "；".join(reasons),
        "rms_dlat": round(rms, 3),
        "p95_abs_dlat": round(svals[min(n - 1, int(n * 0.95))], 3),
        "max_abs_dlat": round(svals[-1], 3),
        "mean_dlat": round(sum(r[1] for r in rows) / n, 3),
        "inside_pct": (round(100.0 * sum(1 for v in ins_known if v)
                             / len(ins_known), 1) if ins_known else None),
        "drift_m": round(drift, 1),
        "start_arc_m": round(start_arc, 1),
        "start_gap_m": round(start_gap, 1),
        "end_gap_m": round(end_gap, 1),
        "ref_self_cross_m": (round(self_cross, 1)
                             if self_cross != float("inf") else None),
        "line": line,
        "grid_m": grid,
        "segments": [],
        "worst_win": None,
        "offtrack_runs": [],
    }
    # 不可信就不给分段与最差段：一组标了「不可比」的数字本身就没意义
    if not reliable:
        return out

    # 出界段：|dlat| > offtrack_m 的连续帧聚段。用**本圈累计距离**记端点
    # （调用方插值回时间），最短 8 m 过滤单帧抖动。
    runs = []
    run = None
    for r in rows:
        if abs(r[1]) > offtrack_m:
            if run is None:
                run = {"from_dist_m": r[5], "to_dist_m": r[5],
                       "max_abs_dlat": abs(r[1]),
                       "from_s_m": r[0], "to_s_m": r[0]}
            else:
                run["to_dist_m"] = r[5]
                run["to_s_m"] = r[0]
                run["max_abs_dlat"] = max(run["max_abs_dlat"], abs(r[1]))
        else:
            if run is not None and run["to_dist_m"] - run["from_dist_m"] >= 8.0:
                runs.append(run)
            run = None
    if run is not None and run["to_dist_m"] - run["from_dist_m"] >= 8.0:
        runs.append(run)
    out["offtrack_runs"] = [{
        "from_dist_m": round(x["from_dist_m"], 1),
        "to_dist_m": round(x["to_dist_m"], 1),
        "max_abs_dlat": round(x["max_abs_dlat"], 2),
        "from_s_m": round(x["from_s_m"], 1),
        "to_s_m": round(x["to_s_m"], 1),
    } for x in runs]

    # 参考线等弧长采样（步长取 ref_len/step 个点，约 1400 个），
    # 给前端画「灰线底图 + 彩色偏差」用。strided 采样即可，
    # 原始 R 是 ~50 Hz，已经很密。
    ref_step = max(1, round(len(R) * step / ref_len))
    ref_line = [[round(R[k][1], 2), round(R[k][2], 2)]
                for k in range(0, len(R), ref_step)]
    out["ref_line"] = ref_line

    # 分 10 段给均值——用户要的是「在哪个区段偏得最多」，不是全圈一个数
    segs = []
    seg_len = ref_len / 10.0
    for k in range(10):
        a, b = k * seg_len, (k + 1) * seg_len
        win = [r for r in rows if a <= r[0] < b]
        if not win:
            continue
        wv = [abs(r[1]) for r in win]
        segs.append({
            "from_m": round(a),
            "to_m": round(b),
            "mean_dlat": round(sum(r[1] for r in win) / len(win), 3),
            "max_abs_dlat": round(max(wv), 3),
            "rms_dlat": round(math.sqrt(sum(v * v for v in wv) / len(wv)), 3),
        })
    out["segments"] = segs
    if segs:
        w = max(segs, key=lambda x: x["rms_dlat"])
        out["worst_win"] = {"from_m": w["from_m"], "to_m": w["to_m"],
                            "rms_dlat": w["rms_dlat"],
                            "mean_dlat": w["mean_dlat"]}
    return out


# —— 赛道指纹（自动识别赛道） ————————————————

_FP_POINTS = 200        # 描述子点数：等弧长重采样
_FP_LEN_TOL = 0.05      # 代表圈圈长容差（相对中位数）
_FP_MIN_PTS = 100       # 一圈至少多少个有效坐标点才参与


def _fp_path_len(pts: list[tuple[float, float]]) -> float:
    """几何路径长度（米）——注意是**坐标折线**长度，不是速度积分距离。

    出赛道/打转会让折线长度明显变大；残缺圈则偏短。两者都能被
    「圈长相对中位数 ±tol」这道过滤拦住（与 sector_times 的残缺圈过滤同源）。
    """
    return sum(math.dist(pts[i - 1], pts[i]) for i in range(1, len(pts)))


def _fp_resample(pts: list[tuple[float, float]], n: int):
    """等弧长重采样成 n 个点。总长 <=0 返回 None。"""
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + math.dist(pts[i - 1], pts[i]))
    total = cum[-1]
    if total <= 0:
        return None
    out = []
    j = 0
    for k in range(n):
        target = total * k / n
        while j + 1 < len(cum) and cum[j + 1] < target:
            j += 1
        if j + 1 >= len(cum):
            out.append(pts[-1])
            continue
        seg = cum[j + 1] - cum[j]
        w = 0.0 if seg <= 0 else (target - cum[j]) / seg
        (x0, z0), (x1, z1) = pts[j], pts[j + 1]
        out.append((x0 + (x1 - x0) * w, z0 + (z1 - z0) * w))
    return out


def _fp_signed_area(pts) -> float:
    """带符号面积：正 = 逆时针，负 = 顺时针（鞋带公式）。"""
    s = 0.0
    for i in range(len(pts)):
        x0, z0 = pts[i - 1]
        x1, z1 = pts[i]
        s += x0 * z1 - x1 * z0
    return s / 2.0


def _fp_normalize(pts):
    """归一化：质心对齐 → RMS 半径归一 → 起点旋到 +X → 绕行方向统一逆时针。

    🔴 起点对齐**不需要旋转搜索**：同一赛道上车永远从同一物理位置过线，
       所以两个场次的起点天然对应。这是这套指纹又快又稳的前提；
       track_deviation 的 start_gap 护栏依赖的是同一前提。
       反向跑（同一条赛道反方向）在「镜像成逆时针」那步被拉齐——
       形状相同就命中同一条，想区分看 meta.turns_cw。
    """
    n = len(pts)
    cx = sum(p[0] for p in pts) / n
    cz = sum(p[1] for p in pts) / n
    c = [(x - cx, z - cz) for x, z in pts]

    area = _fp_signed_area(c)
    turns_cw = area < 0
    if turns_cw:
        c = [(x, -z) for x, z in c]

    rms = math.sqrt(sum(x * x + z * z for x, z in c) / n)
    if rms <= 0:
        return None, None
    c = [(x / rms, z / rms) for x, z in c]

    sx, sz = c[0]
    ang = math.atan2(sz, sx)
    ca, sa = math.cos(-ang), math.sin(-ang)
    c = [(x * ca - z * sa, x * sa + z * ca) for x, z in c]

    meta = {"turns_cw": turns_cw, "rms_radius": None}
    return c, meta


def track_fingerprint(grouped: dict[int, list[dict]],
                      n_points: int = _FP_POINTS,
                      tol: float = _FP_LEN_TOL) -> dict:
    """从按圈分组的帧里提取**赛道形状指纹**（自动识别赛道用）。

    GT7 协议不下发赛道名（jsonl 表头 circuit 恒为 null），但轨迹形状稳定：
    同一赛道两个场次的归一化指纹 RMS 距离 ~0.006，不同赛道最近也有 ~0.24
    （7 场实测，间隔 43 倍）——阈值好定，识别可靠。

    代表圈挑选（两道过滤，与 sector_times / track_deviation 同源）：
      1. 圈长（坐标折线长度）相对中位数 ±tol —— 挡掉出赛道/打转/残缺圈；
         中位数不取最大值，理由见 sector_times。
      2. 幸存圈里挑**用时最短**的——最快圈走线最干净，最能代表赛道形状。

    返回 {desc: [[x,z]×n_points], ref_len_m, turns_cw, lap, n_laps, n_full}
    或 {error}。desc 已归一化，与平移/尺度/旋转/绕行方向无关。
    """
    if not grouped:
        return {"error": "没有可用的圈"}
    info = []
    for lap_no, fs in grouped.items():
        pts = [(f.get("car_x"), f.get("car_z")) for f in fs]
        pts = [(x, z) for x, z in pts if x is not None and z is not None]
        if len(pts) < _FP_MIN_PTS:
            continue
        ln = _fp_path_len(pts)
        if len(fs) < 2 or fs[-1].get("t") is None or fs[0].get("t") is None:
            continue
        dur = (fs[-1]["t"] or 0.0) - (fs[0]["t"] or 0.0)
        if ln <= 0 or dur <= 0:
            continue
        info.append((lap_no, pts, ln, dur))
    if not info:
        return {"error": "没有足够长的圈"}

    lens = sorted(i[2] for i in info)
    ref = lens[len(lens) // 2]
    full = [i for i in info if abs(i[2] - ref) <= ref * tol]
    if not full:
        full = info
    lap_no, xy, ln, dur = min(full, key=lambda i: i[3])

    rs = _fp_resample(xy, n_points)
    if not rs:
        return {"error": "重采样失败"}
    desc, _meta = _fp_normalize(rs)
    if desc is None:
        return {"error": "归一化失败"}
    return {"desc": [[round(x, 4), round(z, 4)] for x, z in desc],
            "ref_len_m": round(ln, 1),
            "turns_cw": _meta["turns_cw"],
            "lap": lap_no, "n_laps": len(info), "n_full": len(full)}


def fingerprint_distance(a: list, b: list) -> float:
    """两条指纹的逐点 RMS 距离（归一化坐标系，无量纲）。"""
    if not a or not b or len(a) != len(b):
        return float("inf")
    return math.sqrt(sum(math.dist(a[i], b[i]) ** 2 for i in range(len(a)))
                     / len(a))


# —— 门面 ——————————————————————————————

def match_pv_pairs(peaks_ref: list[dict], peaks_cur: list[dict],
                   ref_total_m: float, cur_total_m: float,
                   tol_frac: float = 0.012) -> list[dict]:
    """把两圈的关键点配成对（同 kind 才配，圈内相对位置差 ≤ tol_frac）。

    🔴 为什么不用绝对距离配对：两圈的速度积分漂移可达上百米
       （实测同场 3/4 圈同弯道相差 60~300m），绝对距离 30m 容差
       只配上 6/17；改用「圈长分数」（相对位置）配 11/17，
       且位置差 <1.1%，弯道对得整整齐齐。

    返回 [{kind, distance, distance_cur, speed_ref, speed_cur, delta}, ...]
    （distance 取参考圈的，delta = 最新圈 − 参考圈，正=最新圈更快）
    """
    pairs: list[dict] = []
    if ref_total_m <= 0 or cur_total_m <= 0:
        return pairs
    taken: set[int] = set()
    for rv in peaks_ref:
        rf = rv["distance"] / ref_total_m
        best: dict | None = None
        best_f = 1e9
        for cv in peaks_cur:
            if cv["kind"] != rv["kind"] or id(cv) in taken:
                continue
            f = abs(cv["distance"] / cur_total_m - rf)
            if f < best_f:
                best_f, best = f, cv
        if best is not None and best_f <= tol_frac:
            taken.add(id(best))
            pairs.append({
                "kind": rv["kind"],
                "distance": rv["distance"],
                "distance_cur": best["distance"],
                "speed_ref": rv["speed_kph"],
                "speed_cur": best["speed_kph"],
                "delta": round(best["speed_kph"] - rv["speed_kph"], 1),
            })
    pairs.sort(key=lambda p: p["distance"])
    return pairs


def analyze_compare(frames: list[dict] | None = None, ref_lap_no: int | None = None,
                    cur_lap_no: int | None = None, step: float = 10.0,
                    grouped: dict[int, list[dict]] | None = None,
                    official: dict[int, float] | None = None) -> dict:
    """门面：分组 → 每圈采样 → 选参考圈/对比圈 → 时间差 + 峰谷 + 赛车线。

    `ref_lap_no` 缺省 = 最快圈；`cur_lap_no` 缺省 = 最后一圈（圈号最大的有效圈）。
    两者都允许由调用方（HTTP 查询参数 / UI 下拉）指定，这样用户可以
    任选两圈对比，而不是只能拿最后一圈跟最快圈比。

    `grouped` 可选：调用方若手上已经有 clean_laps 的结果（例如详情页从
    _valid_laps 的记忆化缓存里取），直接传进来即可 —— 此时 `frames` 可以不传。
    详情页一次渲染里 analyze_session 与这里都要同一份分组，不共用就是白扫
    21 万帧两遍。

    `official` 可选：官方圈速映射（lap → 秒）。调用方若有**原始分组**算出的
    版本（含 clean_laps 丢弃的末圈）就传进来，否则本函数按清洗后的分组算，
    最后一圈会退回帧跨度。

    clean_laps 会剔除首/末假圈（前圈、完赛离场圈）与菜单态，
    否则 25s 的完赛余圈会被当成「最快圈」画出离场的小段赛车线。
    本函数再按**圈长**（0.90~1.25 × 基准圈长）过滤一遍：中间圈的残圈
    clean_laps 不删（那可能是一次真实的事故圈），只能在这里挡——
    否则一段跑了 55% 的圈会以「更短的时长」被选成参考圈。
    """
    laps = clean_laps(frames) if grouped is None else grouped
    # 圈完整性：距离口径（与 clean_laps / analyze_session 同一判据）。
    # 只过滤「帧数 ≥30」不够：一次中途退出可能跑了 55% 圈长、60 多秒，
    # 帧数完全够，时长也够，于是被当成最快圈。med==0（圈数 <3）时
    # lap_is_complete 返回 True，保持旧行为。
    dists, med_d = lap_distances(laps)
    # 🔴 只保留「有效圈」的帧，而且**只为最终选中的两圈**建采样点。
    #    旧实现给每一圈都调 lap_samples（每帧一个 10 键 dict），
    #    20 圈 10 万帧实测 1.75s / 73MB，而结果里只用到 ref/cur 两圈。
    valid = {n: fs for n, fs in laps.items()
             if len(fs) >= 30
             and lap_is_complete(dists.get(n, 0.0), med_d)}
    if not valid:
        return {"laps_analyzed": 0, "lap_summary": [], "ref_lap": None,
                "cur_lap": None, "time_diff": {"step": step, "grid": [],
                                               "diff_ms": []},
                "peaks_ref": [], "peaks_cur": [], "pv_pairs": [],
                "race_line": {"segments": []}}

    # 摘要只需要 4 个标量，逐帧流式算即可，不必先建 10 键的采样点。
    # 圈速优先用官方 last_lap（帧跨度会把暂停/进站算进去）。
    # `official` 可由调用方传入（含 clean_laps 丢弃的末圈），否则就地算。
    if official is None:
        official = official_lap_times(laps)
    summary = []
    for n, fs in valid.items():
        # 🔴 duration 必须用原始帧跨度（和 analyze_session 的圈速表同一口径）。
        #    之前用重采样最后一点的 t_rel，终点被采样网格截断，
        #    会出现「表格标第 6 圈最快、选择器默认第 8 圈」的对不上。
        span = (fs[-1].get("t") or 0.0) - (fs[0].get("t") or 0.0)
        off = official.get(n)
        dur = off if off is not None else span
        speeds = [(f.get("speed_kph") or 0.0) for f in fs]
        summary.append({
            "lap": n, "duration_s": round(dur, 3),
            "span_s": round(span, 3),
            "time_source": "official" if off is not None else "span",
            # 与 lap_samples 末尾的 dist 同源（都是速度积分），round 后一致
            "distance_m": round(dists.get(n, 0.0), 1),
            "max_speed_kph": max(speeds),
            "avg_speed_kph": round(sum(speeds) / len(speeds), 1),
        })
    summary.sort(key=lambda s: s["lap"])
    fastest = min(summary, key=lambda s: s["duration_s"])["lap"]
    # 指定的圈号必须真实有效（URL 可能被手改成任意值），
    # 否则回退到默认值，避免 KeyError 把整个详情页打挂。
    if ref_lap_no is None or ref_lap_no not in valid:
        ref_lap_no = fastest
    if cur_lap_no is None or cur_lap_no not in valid:
        cur_lap_no = max(valid)
    # 参考圈与对比圈撞在一起时（用户手选了同一圈，或该场只有一圈被保留），
    # 退到「另一圈」；实在没有别的圈就保持原样（单圈场次时间差自然是全 0）。
    if cur_lap_no == ref_lap_no and len(valid) > 1:
        others = [n for n in sorted(valid) if n != ref_lap_no]
        cur_lap_no = others[-1]

    ref_pts = lap_samples(valid[ref_lap_no])
    cur_pts = (ref_pts if cur_lap_no == ref_lap_no
               else lap_samples(valid[cur_lap_no]))
    r = time_diff(cur_pts, ref_pts, step)
    peaks_ref = find_peaks_valleys(ref_pts)
    peaks_cur = find_peaks_valleys(cur_pts)
    # 圈长：采样点最后一帧的累计距离（m）
    ref_total = ref_pts[-1]["dist"]
    cur_total = cur_pts[-1]["dist"]
    return {
        "laps_analyzed": len(valid),
        "lap_summary": summary,
        "ref_lap": ref_lap_no,
        "cur_lap": cur_lap_no,
        "time_diff": r,
        "peaks_ref": peaks_ref,
        "peaks_cur": peaks_cur,
        "pv_pairs": match_pv_pairs(peaks_ref, peaks_cur, ref_total, cur_total),
        "race_line": race_line(ref_pts),
    }


# ===========================================================================
# 圈剖面（lap profile）—— 供赛道工程师/第三方按「赛道位置」索引一圈
# ===========================================================================
#
# 🔴 与上面所有函数最本质的区别：距离轴改用**几何弧长**，不是速度积分。
#
#    速度积分（`dist += v·Δt`）一圈漂移 **60~300 m**（实测，见 track_deviation）。
#    拿它做「本圈跑到 1200 m」和「参考圈的 1200 m」对齐，实际赛道上能差十几米 ——
#    刹车点预告会直接报错位置。几何弧长只依赖 car_x/car_z 的相邻弦长，不漂移。
#
#    旧口径全部保留（`dist` 还是速度积分），本模块新增 `s_geo` 通道；
#    `/profile` 端点默认用几何弧长，并把两者的差值当诊断指标报出来。

_ARC_GAP_M = 100.0   # 单帧位移超过这个数（>3600 km/h）判为坐标跳变，不计入弧长


def lap_arc_length(pts: list[dict]) -> tuple[list[float], bool]:
    """把 lap_samples 的点序列转成几何弧长（米）。

    返回 `(弧长列表, 是否真的用了几何)`。
    坐标缺失（None / 整圈全 0）时退化为速度积分的 `dist`，并返回 False ——
    调用方**必须**把这个降级暴露出去，不能假装还是几何口径。
    """
    if not pts:
        return [], False
    xs = [p.get("x") for p in pts]
    zs = [p.get("z") for p in pts]
    vals = xs + zs
    usable = all(isinstance(v, (int, float)) for v in vals)
    if usable:
        usable = (max(abs(v) for v in xs)
                  + max(abs(v) for v in zs)) > 1e-6
    if not usable:
        return [float(p.get("dist") or 0.0) for p in pts], False

    out = [0.0]
    acc = 0.0
    prev_x, prev_z = xs[0], zs[0]
    for i in range(1, len(pts)):
        cx, cz = xs[i], zs[i]
        step = math.hypot(cx - prev_x, cz - prev_z)
        if step < _ARC_GAP_M:
            acc += step
        prev_x, prev_z = cx, cz
        out.append(acc)
    return out, True


def _nearest_index(dists: list[float], d: float) -> int:
    """dists（升序）里最接近 d 的下标。"""
    if not dists:
        return 0
    i = bisect.bisect_left(dists, d)
    if i <= 0:
        return 0
    if i >= len(dists):
        return len(dists) - 1
    return i if (dists[i] - d) < (d - dists[i - 1]) else i - 1


def _find_brake_zones(pts: list[dict], s: list[float],
                      thr: float = 0.2, min_gap_frames: int = 3,
                      min_dur_s: float = 0.15) -> list[dict]:
    """刹车区：brake >= thr 的连续段。段内允许 min_gap_frames 帧的断续（噪声）。"""
    out: list[dict] = []
    n = len(pts)
    i = 0
    while i < n:
        if (pts[i].get("brake") or 0.0) < thr:
            i += 1
            continue
        j, gap = i, 0
        while j + 1 < n:
            if (pts[j + 1].get("brake") or 0.0) >= thr:
                j += 1
                gap = 0
            elif gap < min_gap_frames:
                j += 1
                gap += 1
            else:
                break
        j -= gap
        if j < i:
            j = i
        dur = (pts[j].get("t_rel") or 0.0) - (pts[i].get("t_rel") or 0.0)
        if dur >= min_dur_s:
            seg = pts[i:j + 1]
            out.append({
                "s_in_m": round(s[i], 1),
                "s_out_m": round(s[j], 1),
                "speed_in_kph": round(pts[i].get("speed_kph") or 0.0, 1),
                "peak_brake": round(max(p.get("brake") or 0.0 for p in seg), 3),
                "duration_s": round(dur, 3),
                "v_min_kph": round(min(p.get("speed_kph") or 0.0 for p in seg), 1),
            })
        i = j + 1
    return out


def lap_profile(frames: list[dict], lap_no: int = 0, step_m: float = 5.0,
                prominence_kph: float = 12.0, max_points: int = 3000) -> dict:
    """一圈的「按赛道位置索引」剖面 —— 一次性喂饱赛道工程师。

    产出三样东西：
      1. **几何折线** `pt.x` / `pt.z`   —— 消费方用它做实时最近点定位
      2. **等距网格上的遥测** `speed_kph` / `throttle` / `brake` / `t_rel_s` / `glat`
      3. **关键点** `markers`          —— 刹车入点 / 弯心（半径+左右）/ 给油点

    这样消费方（gt7-coach）不需要自己从头攒图：不用等第一圈跑完、
    不用被 10Hz 的实时轮询限精度、也不用重写一遍峰谷检测。

    `frames` 必须是**同一圈**的帧（用 clean_laps/split_laps 切好）。
    """
    # 这里必须用**速度积分**口径：本函数的诊断就是「积分 vs 几何」的漂移
    pts = lap_samples(frames, geo=False)
    if len(pts) < 20:
        return {"error": "帧数不足，无法建立圈剖面", "frames": len(pts)}

    s_geo, geo_ok = lap_arc_length(pts)
    total = s_geo[-1]
    if total < 200.0:
        return {"error": "圈长不足 200 m", "length_m": round(total, 1),
                "geometry_used": geo_ok}

    # 抽稀保护：小步长 + 长赛道会产出上万点，按需放大步长
    step = max(1.0, float(step_m))
    if total / step > max_points:
        step = total / max_points
    grid: list[float] = []
    d = 0.0
    while d < total:
        grid.append(round(d, 1))
        d += step

    def col(key: str, ndigits: int = 2, cast=float) -> list:
        vals: list[float] = []
        for p in pts:
            v = p.get(key)
            vals.append(float(v) if isinstance(v, (int, float)) else 0.0)
        return [round(_interp(s_geo, vals, g), ndigits) for g in grid]

    # 几何可能缺失（有些场次没坐标）→ 折线给空列表，而不是塞一串 0
    xs = [p.get("x") for p in pts]
    zs = [p.get("z") for p in pts]
    have_geo = geo_ok and all(isinstance(v, (int, float)) for v in xs + zs)

    # 峰谷（在几何距离轴上重跑，而不是在速度积分轴上）
    pts_geo = [{**p, "dist": si} for p, si in zip(pts, s_geo)]
    extrema = find_peaks_valleys(pts_geo, prominence_kph=prominence_kph)

    # 弯心：谷点 + 由横向 G 反推的半径与左右
    # κ = G_lat · g / v²；半径 = 1/|κ|，|κ| 太小时视为直线（不给半径）
    apexes: list[dict] = []
    for e in extrema:
        if e.get("kind") != "valley":
            continue
        k = _nearest_index(s_geo, e["distance"])
        v_ms = max(float(pts[k].get("v") or 0.0), 1.0)
        glat = float(pts[k].get("glat") or 0.0)
        kappa = glat * 9.80665 / (v_ms * v_ms)
        item: dict = {"s_m": round(e["distance"], 1),
                      "speed_kph": round(e["speed_kph"], 1),
                      "glat": round(glat, 3)}
        if abs(kappa) > 5e-4:
            item["radius_m"] = round(1.0 / abs(kappa), 1)
            item["turn"] = "左" if glat > 0 else "右"
        else:
            item["radius_m"] = None
            item["turn"] = "直线"
        apexes.append(item)

    # 出弯给油点：每个弯心之后第一次 throttle >= 0.5
    thr_grid = col("throttle", 3)
    spd_grid = col("speed_kph", 1)
    throttle_on: list[dict] = []
    for a in apexes:
        start = _nearest_index(grid, a["s_m"])
        for gi in range(start, len(grid)):
            if thr_grid[gi] >= 0.5:
                throttle_on.append({
                    "s_m": grid[gi],
                    "speed_kph": spd_grid[gi],
                    "after_apex_m": round(grid[gi] - a["s_m"], 1),
                })
                break

    warnings: list[str] = []
    if not geo_ok:
        warnings.append("本圈无坐标，距离轴降级为速度积分（会漂移），"
                        "实时定位精度不可保证")
    drift = None
    if total > 0:
        drift = round((pts[-1]["dist"] - total) / total * 100.0, 2)
        if abs(drift) > 3.0:
            warnings.append(f"速度积分与几何弧长相差 {drift}%"
                            f"（{pts[-1]['dist'] - total:.0f} m），已按几何口径输出")

    return {
        "lap": lap_no,
        "frames": len(pts),
        # —— 两种圈长口径都给出，差值就是诊断指标 ——
        "length_m": round(total, 1),
        "length_by_speed_m": round(pts[-1]["dist"], 1),
        "length_drift_pct": drift,
        "geometry_used": geo_ok,
        "lap_time_s": round(pts[-1]["t_rel"], 3),
        "step_m": round(step, 2),
        # —— 等距网格 ——
        "grid_m": grid,
        "speed_kph": col("speed_kph", 1),
        "throttle": thr_grid,
        "brake": col("brake", 3),
        "t_rel_s": col("t_rel", 3),
        "glat": col("glat", 3),
        "glon": col("glong", 3),
        "pt": {
            "x": [round(_interp(s_geo, [float(v) for v in xs], g), 2)
                  for g in grid] if have_geo else [],
            "z": [round(_interp(s_geo, [float(v) for v in zs], g), 2)
                  for g in grid] if have_geo else [],
        },
        "markers": {
            "brake_in": _find_brake_zones(pts, s_geo),
            "apex": apexes,
            "throttle_on": throttle_on,
            # 原始极值也给出来，方便前端自己画「直线段 / 减速段」
            "peak": [e for e in extrema if e.get("kind") == "peak"],
            "valley": [e for e in extrema if e.get("kind") == "valley"],
        },
        "warnings": warnings,
    }


# ===========================================================================
# GT7 赛道表（内置）
# ===========================================================================
#
# 🔴 为什么要内置一张表：GT7 协议**不下发赛道名**（jsonl 表头 `circuit` 恒为
#    null，实测确认），所以"这条赛道叫什么 / 多长 / 几个弯"只能外部给。
#    表来自社区整理的 ddm999/gt7info → `_data/db/course.csv`
#    （字段 ID / Name / Length(m) / NumCorners / IsReverse / Category），
#    这里只保留识别用得到的列，共 122 条。
#    内置而不是运行时下载：跑在纯内网（PS5 + 本地服务器，无出海）也要能认赛道，
#    而且这点数据量没必要每次走网络。
#
# 匹配策略（两步，成本从低到高）：
#   1. `course_candidates()` 用**圈长**粗筛（默认 ±3%）——圈长是极强的一维
#      特征：122 条里通常只剩 2~5 条候选（同一条赛道的多个布局长度相同，
#      会一起留下，这正好是我们要的"疑似列表"）。
#   2. 需要定案时再用本文件已有的 `track_fingerprint` 归一化形状比对 ——
#      它对平移/尺度/旋转/绕行方向都不变，同赛道 ~0.006、不同赛道 ~0.24。
_COURSE_CSV = """\
ID,Name,Length,NumCorners,IsReverse,Category
4,Daytona Tri-Oval,4023,4,0,circuit
10,Suzuka Circuit,5807,20,0,circuit
11,High Speed Ring,4345,6,0,original
16,Fuji International Speedway,4563,16,0,circuit
41,WeatherTech Raceway Laguna Seca,3602,11,0,circuit
51,Deep Forest Raceway,4253,18,0,original
95,Nurburgring Nordschleife,20832,73,0,circuit
116,Mount Panorama Motor Racing Circuit,6213,23,0,circuit
119,Brands Hatch Grand Prix Circuit,3946,9,0,circuit
122,Willow Springs International Raceway: Big Willow,3951,10,0,circuit
123,Willow Springs International Raceway: Streets of Willow Springs,2675,14,0,circuit
125,Willow Springs International Raceway: Horse Thief Mile,1624,11,0,circuit
152,Autodromo de Interlagos,4309,15,0,circuit
154,Goodwood Motor Circuit,3809,7,0,circuit
346,Brands Hatch Indy Circuit,1944,5,0,circuit
347,Nurburgring Nordschleife Tourist Layout,20832,73,0,circuit
348,Nurburgring 24h,25378,89,0,circuit
349,Nurburgring GP,5148,17,0,circuit
351,Tokyo Expressway - Central Clockwise,4405,13,0,city
353,Fishermans Ranch,6893,40,0,snow_dirt
354,Northern Isle Speedway,900,4,0,original
356,Blue Moon Bay Speedway,3200,3,0,original
357,BB Raceway,1665,5,0,original
358,Alsace - Village,5423,17,0,original
361,Tokyo Expressway - East Clockwise,7301,13,0,city
362,Tokyo Expressway - South Clockwise,5201,17,0,city
363,Dragon Trail - Seaside,5209,18,0,original
365,Autodrome Lago Maggiore - Full Course,5809,17,0,original
372,Colorado Springs - Lake,2990,15,0,snow_dirt
376,Kyoto Driving Park - Yamagiwa,4912,15,0,original
439,Alsace - Village Reverse,5423,17,1,original
440,Kyoto Driving Park - Miyabi,1953,7,0,original
442,Suzuka Circuit East Course,2243,9,0,circuit
443,Fishermans Ranch Reverse,6893,40,1,snow_dirt
444,BB Raceway Reverse,1665,5,1,original
449,Blue Moon Bay Speedway Reverse,3200,3,1,original
450,Tokyo Expressway - Central Counterclockwise,4369,14,0,city
451,Tokyo Expressway - East Counterclockwise,7192,13,0,city
452,Willow Springs International Raceway: Horse Thief Mile Reverse,1624,11,1,circuit
453,Sardegna - Windmills,3310,14,0,snow_dirt
454,24 Heures du Mans Racing Circuit,13629,38,0,circuit
462,Circuit de Spa-Francorchamps,7004,21,0,circuit
469,Autodromo Nazionale Monza,5793,11,0,circuit
470,Red Bull Ring,4318,10,0,circuit
471,Tsukuba Circuit,2045,8,0,circuit
598,Willow Springs International Raceway: Streets of Willow Springs Reverse,2675,14,1,circuit
599,Sardegna - Windmills Reverse,3310,14,1,snow_dirt
600,Colorado Springs - Lake Reverse,2990,15,1,snow_dirt
601,Dragon Trail - Seaside Reverse,5209,18,1,original
602,Autodrome Lago Maggiore - Full Course Reverse,5809,17,1,original
613,Kyoto Driving Park - Yamagiwa Reverse,4912,15,1,original
742,Autodromo Nazionale Monza No Chicane,5755,9,0,circuit
747,Deep Forest Raceway Reverse,4253,18,1,original
776,Kyoto Driving Park - Yamagiwa+Miyabi,6846,19,0,original
777,Kyoto Driving Park - Yamagiwa+Miyabi Reverse,6846,19,1,original
790,Autodrome Lago Maggiore - East End Reverse,2010,8,1,original
791,Autodrome Lago Maggiore - Centre Reverse,1706,8,1,original
792,Autodrome Lago Maggiore - West End,2444,10,0,original
793,Autodrome Lago Maggiore - Centre,1706,8,0,original
794,Autodrome Lago Maggiore - West End Reverse,2444,10,1,original
795,Autodrome Lago Maggiore - East End,2010,8,0,original
819,Blue Moon Bay Speedway - Infield B,2860,5,0,original
820,Blue Moon Bay Speedway - Infield A,3350,10,0,original
822,Blue Moon Bay Speedway - Infield A Reverse,3350,10,1,original
823,Blue Moon Bay Speedway - Infield B Reverse,2860,5,1,original
825,Circuit de Sainte-Croix - A,9477,19,0,original
826,Circuit de Sainte-Croix - A Reverse,9477,19,1,original
827,Circuit de Sainte-Croix - B,7062,14,0,original
828,Circuit de Sainte-Croix - B Reverse,7062,14,1,original
829,Circuit de Sainte-Croix - C,10825,24,0,original
830,Circuit de Sainte-Croix - C Reverse,10825,24,1,original
837,Fuji International Speedway (Short),4526,14,0,circuit
838,Sardegna - Road Track - C Reverse,2661,10,1,original
839,Sardegna - Road Track - B Reverse,3893,13,1,original
841,Sardegna - Road Track - B,3893,13,0,original
843,Sardegna - Road Track - C,2661,10,0,original
846,Red Bull Ring Short Track,2336,6,0,circuit
847,Dragon Trail - Gardens,4352,17,0,original
853,Dragon Trail - Gardens Reverse,4352,17,1,original
854,24 Heures du Mans Racing Circuit No Chicane,13567,32,0,circuit
874,Circuit de Barcelona-Catalunya Grand Prix Layout,4655,16,0,circuit
896,Autopolis International Racing Course,4674,18,0,circuit
898,Autopolis International Racing Course - Short Course,3022,11,0,circuit
915,Tokyo Expressway - South Counterclockwise,6560,16,0,city
921,Sardegna - Road Track - A,5113,15,0,original
922,Sardegna - Road Track - A Reverse,5113,15,1,original
1024,Trial Mountain Circuit,5434,15,0,original
1034,Trial Mountain Circuit - Reverse,5434,15,1,original
1069,High Speed Ring Reverse,4345,6,1,original
1163,Daytona Road Course,5729,12,0,circuit
1224,Alsace - Test Course,2118,7,0,original
1225,Alsace - Test Course Reverse,2118,7,1,original
1226,Autodrome Lago Maggiore - East,3643,11,0,original
1231,Autodrome Lago Maggiore - East Reverse,3643,11,1,original
1232,Autodrome Lago Maggiore - West,4168,13,0,original
1233,Autodrome Lago Maggiore - West Reverse,4168,13,1,original
1260,Special Stage Route X,30283,2,0,original
1261,Special Stage Route X 400,30283,0,0,original
1262,Special Stage Route X 1000,30283,0,0,original
1269,Spa 24h Layout,7004,21,0,circuit
1240,Watkins Glen Long Course,5423,11,0,circuit
1264,Watkins Glen Short Course,3942,7,0,circuit
1249,Circuit de Barcelona-Catalunya GP Layout No Chicane,4730,14,0,circuit
1250,Circuit de Barcelona-Catalunya National,2977,11,0,circuit
1265,Circuit de Barcelona-Catalunya Rallycross,1133,11,0,snow_dirt
1247,Nurburgring Endurance,23864,85,0,circuit
1248,Nurburgring Sprint,3629,12,0,circuit
1292,Grand Valley - Highway 1,5099,18,0,circuit
1293,Grand Valley - Highway 1 Reverse,5099,18,1,circuit
1299,Grand Valley - South,3076,10,0,circuit
1300,Grand Valley - South Reverse,3076,10,1,circuit
1007,Michelin Raceway Road Atlanta,4088,12,0,circuit
1273,Lake Louise Tri-Oval,3068,3,0,snow_dirt
1308,Lake Louise Short Track,2577,7,0,snow_dirt
1309,Lake Louise Long Track,3694,11,0,snow_dirt
1316,Lake Louise Tri-Oval Reverse,3068,3,1,snow_dirt
1317,Lake Louise Short Track Reverse,2577,7,1,snow_dirt
1318,Lake Louise Long Track Reverse,3694,11,1,snow_dirt
2001,Eiger Nordwand,2436,11,0,original
2002,Eiger Nordwand Reverse,2436,11,1,original
2003,Yas Marina Circuit,5281,16,0,circuit
2004,Circuit Gilles-Villeneuve,4361,14,0,circuit
"""

_COURSE_CACHE: list[dict] | None = None

# 赛道 BoP 强度（`course-bop` 页面的人工标注，91 条，按 course.csv 的 ID 键）。
# 🔴 BoP = Balance of Performance，GT7 线上赛对某些赛道做的性能平衡，分
#    高速/中速/低速三档。它**不是**赛道属性而是赛事属性，且官方后来不再区分
#    各档具体数值（gt7info 原话："may be missing newer tracks"），所以这里
#    只当参考标签展示，不参与任何计算。
# 🔴 只有 91/122 条：没标的赛道 `bop` 为空，前端拿到空就不渲染徽章。
_TRACK_BOP = (
    "4:High,10:Mid,11:High,16:Mid,41:Low,51:High,95:Mid,116:High,119:Mid,"
    "122:Mid,123:Low,125:Low,152:Mid,154:Mid,346:Low,348:Mid,349:Mid,351:Mid,"
    "354:High,356:High,357:High,358:Low,361:High,362:High,363:Mid,365:Mid,"
    "376:Mid,439:Low,440:Mid,442:Mid,444:High,449:High,450:Mid,451:High,"
    "454:High,462:Mid,469:High,470:Mid,471:Low,601:Mid,602:Mid,613:Mid,"
    "742:High,747:High,776:Mid,777:Mid,790:Mid,791:Low,792:Mid,793:Low,"
    "794:Mid,795:Mid,819:High,820:Mid,822:Mid,823:High,825:Mid,826:Mid,"
    "827:Mid,828:Mid,829:High,830:High,837:Mid,838:Low,839:Low,841:Low,"
    "843:Low,846:Mid,847:Mid,853:Mid,854:High,874:Mid,896:Mid,898:Mid,"
    "915:High,921:Mid,922:Mid,1024:Mid,1034:Mid,1069:High,1163:High,"
    "1224:Mid,1225:Mid,1226:Low,1231:Low,1232:Mid,1233:Mid,1240:High,"
    "1260:High,1264:High,1269:Mid"
)

_TRACK_LOGO = (
    # 赛道 ID → 官方 logo 文件名主干（多个布局共用同一个 logo）。
    # 生成：拿 gt7info crsbase.csv 的 Name 当前缀匹配 course 名，少数对不上的
    # （Mount Panorama / Road Atlanta / BB Raceway / Daytona / Spa 24h /
    # Willow Springs）手工补。41 个名字里有 4 个（cgv/eiger/ll/yas）官方站点
    # 还没上图，拉不到就不显示，不影响其它功能。
    "4:daytona,10:suzuka,11:hsr,16:fuji,41:seca,51:deepforest,95:nurb,"
    "116:mtpanorama,119:brands,122:willow,123:willow,125:willow,"
    "152:interlagos,154:goodwood,346:brands,347:nurb,348:nurb,349:nurb,"
    "351:tokyo,353:fishermans,354:northernisle,356:bluemoon,357:bb,358:alsace,"
    "361:tokyo,362:tokyo,363:dragon,365:maggiore,372:colorado,376:kyoto,"
    "439:alsace,440:kyoto,442:suzuka,443:fishermans,444:bb,449:bluemoon,"
    "450:tokyo,451:tokyo,452:willow,453:sardwindmills,454:sarthe,462:spa,"
    "469:monza,470:rbr,471:tsukuba,598:willow,599:sardwindmills,600:colorado,"
    "601:dragon,602:maggiore,613:kyoto,742:monza,747:deepforest,776:kyoto,"
    "777:kyoto,790:maggiore,791:maggiore,792:maggiore,793:maggiore,"
    "794:maggiore,795:maggiore,819:bluemoon,820:bluemoon,822:bluemoon,"
    "823:bluemoon,825:croix,826:croix,827:croix,828:croix,829:croix,830:croix,"
    "837:fuji,838:sardroad,839:sardroad,841:sardroad,843:sardroad,846:rbr,"
    "847:dragon,853:dragon,854:sarthe,874:catalunya,896:autopolis,"
    "898:autopolis,915:tokyo,921:sardroad,922:sardroad,1007:ra,1024:trialmt,"
    "1034:trialmt,1069:hsr,1163:daytona,1224:alsace,1225:alsace,1226:maggiore,"
    "1231:maggiore,1232:maggiore,1233:maggiore,1240:glen,1247:nurb,1248:nurb,"
    "1249:catalunya,1250:catalunya,1260:routex,1261:routex,1262:routex,"
    "1264:glen,1265:catalunya,1269:spa,1273:ll,1292:gv,1293:gv,1299:gv,"
    "1300:gv,1308:ll,1309:ll,1316:ll,1317:ll,1318:ll,2001:eiger,2002:eiger,"
    "2003:yas,2004:cgv"
)

_BOP_CACHE: dict[str, str] | None = None


def bop_map() -> dict[str, str]:
    """赛道 ID → BoP 档位（"High"/"Mid"/"Low"），没标注的键不存在。"""
    global _BOP_CACHE
    if _BOP_CACHE is None:
        out: dict[str, str] = {}
        for seg in _TRACK_BOP.split(","):
            kv = seg.split(":")
            if len(kv) == 2 and kv[0].strip():
                out[kv[0].strip()] = kv[1].strip()
        _BOP_CACHE = out
    return _BOP_CACHE


def bop_of(course_id: int | str) -> str:
    """赛道 ID → BoP 档位，未标注返回空串。"""
    return bop_map().get(str(course_id).strip(), "")


_LOGO_CACHE: dict[str, str] | None = None


def logo_of(course_id: int | str) -> str:
    """赛道 ID → 官方赛道 logo 的**文件名主干**（如 "suzuka" → suzuka.png）。

    122 条赛道全部有映射（同一条赛道的不同布局共用一个 logo）。

    🔴 这里只给名字，**图不随包分发**：logo 是游戏素材，直接进公开仓库有
       版权风险。想要图就用 `helper/download_track_logos.py` 拉到
       `data/tracklogos/`，仪表盘检测到文件存在才渲染；没有就当它不存在。
    """
    global _LOGO_CACHE
    if _LOGO_CACHE is None:
        out: dict[str, str] = {}
        for seg in _TRACK_LOGO.replace("\n", "").split(","):
            kv = seg.split(":")
            if len(kv) == 2 and kv[0].strip():
                out[kv[0].strip()] = kv[1].strip()
        _LOGO_CACHE = out
    return _LOGO_CACHE.get(str(course_id).strip(), "")


def courses() -> list[dict]:
    """内置赛道表（首次调用时解析一次，之后走缓存）。"""
    global _COURSE_CACHE
    if _COURSE_CACHE is not None:
        return _COURSE_CACHE
    lines = _COURSE_CSV.strip().splitlines()
    out: list[dict] = []
    for ln in lines[1:]:
        p = ln.split(",")
        if len(p) < 3:
            continue
        try:
            length = float(p[2])
        except ValueError:
            continue
        if length <= 0:
            continue
        out.append({
            "id": int(p[0]) if p[0].strip().isdigit() else 0,
            "name": p[1].strip(),
            "length_m": length,
            "corners": int(p[3]) if len(p) > 3 and p[3].strip().isdigit() else 0,
            "reverse": p[4].strip() == "1" if len(p) > 4 else False,
            "category": p[5].strip() if len(p) > 5 else "",
            "bop": bop_of(p[0].strip()),
        })
    _COURSE_CACHE = out
    return out


def course_candidates(ref_len_m: float, tol: float = 0.03) -> list[dict]:
    """按圈长粗筛内置赛道表，返回**按差距从小到大**排好的候选。

    `ref_len_m` 用 `track_fingerprint()["ref_len_m"]`（沿轨迹折线实测的圈长）。
    🔴 实测圈长与官方 Length 会有几个百分点的系统性偏差（走的不是同一条线），
       所以容差默认给 3%；太紧（1%）会把正确赛道筛掉，太松（10%）会
       留下一堆候选让人选不过来。
    """
    if not ref_len_m or ref_len_m <= 0:
        return []
    cand = []
    for c in courses():
        d = abs(c["length_m"] - ref_len_m) / ref_len_m
        if d <= tol:
            cand.append({"id": c["id"], "name": c["name"],
                         "length_m": c["length_m"], "corners": c["corners"],
                         "reverse": c["reverse"], "category": c["category"],
                         "bop": c["bop"],
                         "len_err": round(d, 4)})
    cand.sort(key=lambda x: x["len_err"])
    return cand


# ===========================================================================
# 轮胎参考数据（内置，来自 ddm999/gt7info 的社区实测）
# ===========================================================================
#
# 🔴 这是**参考值**，不是遥测量：GT7 的 UDP 遥测**不下发**胎种、天气、
#    赛道积水率，所以下面两组数据都没法用来做实时判断，只能：
#      * 给人看（跑长距离赛前估算进站窗口）
#      * 给教练的定性规则背书（"雨地别用光头胎"这类）
#    谁要是拿它算出"你现在胎还剩 37%"，那一定是编的 —— 遥测里没有这个量。
#
# 数据来源：
#   * `_TYRE_LIFE_CSV` —— gt7info `tyre-wear.md`：把胎跑到失效，记录圈数。
#     两组样本（X2019 800PP @ Blue Moon Bay、原厂 Ford GT3 @ 东京东环），
#     都是 50 倍胎耗。左右前轮分开记：车不同、赛道不同，磨损快的那侧也不同。
#   * `_WET_TYRE_CSV` —— gt7info `wet-tyres.txt`：同一条湿地赛道各胎种的
#     单圈与"开始打滑的积水率"。
_TYRE_LIFE_CSV = """\
Compound,Car,Track,WearRate,LapsFrontRight,LapsFrontLeft,Note
RH,Red Bull X2019 Competition (800PP),Blue Moon Bay Speedway,50x,70,,右前 70 圈后出 T2 失效
RM,Red Bull X2019 Competition (800PP),Blue Moon Bay Speedway,50x,16,22,右前 16 圈、左前 22 圈
RS,Red Bull X2019 Competition (800PP),Blue Moon Bay Speedway,50x,8,11,右前 8 圈、左前 11 圈
IM,Red Bull X2019 Competition (800PP),Blue Moon Bay Speedway,50x,5,6,雨战用胎，寿命极短
W,Red Bull X2019 Competition (800PP),Blue Moon Bay Speedway,50x,4,5,全雨胎寿命最短
IM,Ford GT3 (原厂),Tokyo Expressway - East Clockwise,50x,3,2,重车 + 多弯，比 X2019 更费胎
W,Ford GT3 (原厂),Tokyo Expressway - East Clockwise,50x,2,1,同上
"""

_WET_TYRE_CSV = """\
Compound,RefLap,AquaplaningPct,Note
Comfort Soft,,50,与 Sports Hard 同：积水超过一半就开始打滑
Sports Hard,,50,与 Comfort Soft 同
Sports Soft,2:15.267,50,干地基准圈速
Racing Hard,2:24.114,45,积水 <30% 比 SS 快，>45% 打滑
Intermediate,2:10.917,60,积水 <30% 与 SS 持平并能保持到 50%，>60% 打滑
Heavy Wet,2:12.709,60,积水 <50% 略慢于 SS，但打滑更轻
"""

_TYRE_REF_CACHE: dict | None = None


def tyre_reference() -> dict:
    """轮胎参考数据（寿命 + 湿地特性），结构 ::

        {"life": [{"compound","car","track","wear_rate",
                   "laps_front_right","laps_front_left","note"}, ...],
         "wet":  [{"compound","ref_lap","aquaplaning_pct","note"}, ...],
         "source": "ddm999/gt7info ..."}

    圈数字段可能是 `None`（该样本只记了一侧）。
    """
    global _TYRE_REF_CACHE
    if _TYRE_REF_CACHE is not None:
        return _TYRE_REF_CACHE

    def _int(s: str) -> int | None:
        s = s.strip()
        return int(s) if s.isdigit() else None

    life = []
    for ln in _TYRE_LIFE_CSV.strip().splitlines()[1:]:
        p = ln.split(",")
        if len(p) < 6 or not p[0].strip():
            continue
        life.append({
            "compound": p[0].strip(),
            "car": p[1].strip(),
            "track": p[2].strip(),
            "wear_rate": p[3].strip(),
            "laps_front_right": _int(p[4]),
            "laps_front_left": _int(p[5]),
            "note": p[6].strip() if len(p) > 6 else "",
        })
    wet = []
    for ln in _WET_TYRE_CSV.strip().splitlines()[1:]:
        p = ln.split(",")
        if len(p) < 4 or not p[0].strip():
            continue
        wet.append({
            "compound": p[0].strip(),
            "ref_lap": p[1].strip(),
            "aquaplaning_pct": _int(p[2]),
            "note": p[3].strip(),
        })
    _TYRE_REF_CACHE = {
        "life": life,
        "wet": wet,
        "source": "ddm999/gt7info（tyre-wear.md / wet-tyres.txt，社区实测）",
        # 🔴 这行警告要跟着数据一起走：单独看数字很容易被当成实时量使用。
        "note": "参考值，非遥测量 —— GT7 不下发胎种/天气/积水率，不能用于实时判断",
    }
    return _TYRE_REF_CACHE
