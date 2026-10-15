#!/usr/bin/env python3
"""补齐赛道库用的一键标注工具。

把某个录制场次标注为真实赛道名，写入 gt7-dash 的赛道库（data/tracks.json）。
同赛道（几何指纹在匹配容差内）的所有场次会一起生效。

用法：
    python tools/register_track.py --server http://192.168.43.18:8787 \
        --file "2026-10-15T20-31-04.jsonl.gz" --name "Spa-Francorchamps"

参数：
    --server   gt7-dash 地址（默认 http://192.168.43.18:8787）
    --file     场次文件名（含 .jsonl 或 .jsonl.gz，自动兜底）
    --name     真实赛道名（会写进赛道库）
"""
import argparse
import json
import sys
import urllib.request
import urllib.error


def main() -> int:
    ap = argparse.ArgumentParser(description="标注场次为指定赛道名（补齐赛道库）")
    ap.add_argument("--server", default="http://192.168.43.18:8787",
                    help="gt7-dash 地址")
    ap.add_argument("--file", required=True, help="场次文件名")
    ap.add_argument("--name", required=True, help="真实赛道名")
    args = ap.parse_args()

    url = f"{args.server.rstrip('/')}/api/v1/tracks/register"
    data = json.dumps({"file": args.file, "name": args.name}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)
        except Exception:
            pass
        print(f"[失败] HTTP {e.code}: {detail}", file=sys.stderr)
        return 1
    except Exception as e:  # 网络/超时等
        print(f"[失败] {e}", file=sys.stderr)
        return 1

    if body.get("ok"):
        print(f"[OK] 赛道 #{body.get('track_id')} -> {body.get('name')!r} "
              f"({body.get('hint', '')})")
        return 0
    print(f"[失败] {body}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
