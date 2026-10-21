# -*- coding: utf-8 -*-
"""GT7 Dash 的唯一版本源。

发版流程：
    1. 改这里的 `__version__`（例如 1.3.8）；
    2. 打同名 tag（`v1.3.8`）；
    3. CI 构建镜像 / exe，Release 用 tag 作为发行标识。

不要在其它文件里再写版本号。仪表盘、接收器、Go 解密器、Windows 打包
都从这里取（Go 侧由 CI 用同一个值注入，见 `release-exe.yml`）。

为什么单独一个文件而不是塞进某个模块：
  · `gt7-dashboard.py` / `gt7-recorder.py` 文件名带连字符，不能互相 import；
  · 两者都可能被单独打包成 exe，共享一个纯常量模块最省事；
  · CI 里 `python -c "import gt7_version; print(gt7_version.__version__)"`
    就能拿到版本，不需要解析源码。
"""

__version__ = "1.3.9"
