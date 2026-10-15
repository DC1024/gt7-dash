#!/usr/bin/env python3
"""下载 GT7 官方赛道 logo（41 张 PNG），放到 data/tracklogos/。

用法：python helper/download_track_logos.py [输出目录，缺省 ./data]

结果：data/tracklogos/<logo>.png，仪表盘「赛道地图」页检测到文件存在才显示。

🔴 为什么图不随代码分发：这些 logo 是游戏素材，塞进公开仓库有版权风险。
   所以仓库里只有「赛道 ID → 文件名」的映射（gt7analysis.logo_of），
   图由使用者自己拉下来，不拉也完全不影响其它功能。
   目录已加入 .gitignore，重新部署不会覆盖你已下载的图。

🔴 用 ddm999.github.io 而不是 raw.githubusercontent.com：后者在部分网络
   （含本项目实测环境）根本连不上。
"""
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gt7analysis  # noqa: E402  （要它的 logo_of / courses）

BASE = "https://ddm999.github.io/gt7info/img/track/{}.png"


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
    dst_dir = out_dir / "tracklogos"
    dst_dir.mkdir(parents=True, exist_ok=True)

    names = sorted({gt7analysis.logo_of(c["id"])
                    for c in gt7analysis.courses()})
    names = [n for n in names if n]
    print(f"共 {len(names)} 个赛道 logo → {dst_dir}")

    ok, skip, fail = 0, 0, 0
    for n in names:
        dst = dst_dir / f"{n}.png"
        if dst.exists() and dst.stat().st_size > 0:
            skip += 1
            continue
        try:
            req = urllib.request.Request(BASE.format(n),
                                         headers={"User-Agent": "gt7-dash"})
            data = urllib.request.urlopen(req, timeout=30).read()
        except OSError as e:
            # 🔴 41 个名字里有 4 个（cgv / eiger / ll / yas —— 都是后来加的赛道）
            #    官方站点还没上图，404 是**预期内**的，不是失败。
            code = getattr(e, "code", None)
            print(f"  · {n}: 站点暂无此图（{code or e}），跳过")
            skip += 1
            continue
        # 站点 404 时返回的是 HTML，不是 PNG。写进去会得到一个打不开的图。
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            print(f"  ✗ {n}: 返回的不是 PNG（可能站点已改名），跳过")
            fail += 1
            continue
        dst.write_bytes(data)
        ok += 1

    print(f"✓ 新下载 {ok} 张，跳过 {skip} 张（已存在或站点暂无）"
          + (f"，异常 {fail} 张" if fail else ""))
    return 1 if fail and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
