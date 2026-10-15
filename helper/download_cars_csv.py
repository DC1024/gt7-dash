#!/usr/bin/env python3
"""下载社区维护的 GT7 车辆 ID→车名 对照表（ddm999/gt7info）。

下载到 data/cars.csv 后，仪表盘会把遥测里的 carCode 解析成车名显示。

社区表**会漏车**（新版本新增的车要等社区补录）：实测 3656 就不在表里，
所以另有手工补充表 data/cars_extra.csv，查表时它**优先**于社区表。
本脚本下载完主表后，若目标目录还没有补充表，就把 helper/cars_extra.csv
这份种子复制过去（已存在则不动，保留玩家自己标注的内容）。
用法：python helper/download_cars_csv.py [输出目录，缺省 ./data]
"""
import shutil
import sys
import urllib.request
from pathlib import Path

# 🔴 用官方推荐的「自动更新」地址（站点版）。仓库分支版内容一致但更新慢一拍，
#    而且 raw.githubusercontent.com 在有些网络下根本连不上（本机实测不通，
#    ddm999.github.io 通）。
URL = "https://ddm999.github.io/gt7info/data/db/cars.csv"
SEED_EXTRA = Path(__file__).resolve().parent / "cars_extra.csv"

def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "cars.csv"
    print(f"下载车名对照表 → {dst}")
    req = urllib.request.Request(URL, headers={"User-Agent": "gt7-dash"})
    data = urllib.request.urlopen(req, timeout=30).read()
    dst.write_bytes(data)
    n = len(data.decode("utf-8", "replace").splitlines()) - 1
    print(f"✓ 完成，共 {n} 台车")

    extra = out_dir / "cars_extra.csv"
    if not extra.exists() and SEED_EXTRA.exists():
        shutil.copyfile(SEED_EXTRA, extra)
        k = len(SEED_EXTRA.read_text(encoding="utf-8").splitlines()) - 1
        print(f"✓ 已放入手工补充表种子 {extra}（{k} 台社区表缺的车）")
    else:
        print(f"· 手工补充表 {extra} 已存在，保留原样")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
