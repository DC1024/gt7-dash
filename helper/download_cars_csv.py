#!/usr/bin/env python3
"""下载社区维护的 GT7 车辆 ID→车名 对照表（ddm999/gt7info）。

下载到 data/cars.csv 后，仪表盘会把遥测里的 carCode 解析成车名显示。
用法：python helper/download_cars_csv.py [输出目录，缺省 ./data]
"""
import sys
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/ddm999/gt7info/web-new/_data/db/cars.csv"

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
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
