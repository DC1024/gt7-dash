# GT7 Dash — 跑车浪漫旅 7 实时遥测仪表盘

**PS5《Gran Turismo 7》官方 UDP 遥测 → 本地解密 → Web 实时仪表盘 + 公开数据 API。**
单文件服务、Docker 一键部署、全部数据本地处理、不依赖任何第三方服务。

![GitHub Actions](https://img.shields.io/github/actions/workflow/status/DC1024/gt7-dash/docker.yml?branch=main)
![GHCR](https://img.shields.io/badge/镜像-ghcr.io-blue)
![License](https://img.shields.io/badge/许可-MIT-green)

> **当前版本：v1.4.2**（以 `gt7_version.py` 为唯一版本源）—— 全是工程侧，
> 其中一条**当场抓到一个真 bug**：
>
> - **内联 JS 静态检查**（C2）：`node --check` 只查语法，查不出未定义变量。
>   新加的 ESLint（`no-undef`）第一次跑就发现 `/sessions` 页的回收站列表
>   用了**从未定义的 `esc()`** —— 回收站非空时整张表渲染不出来。
>   规则只开零噪声的那几条；`no-implicit-globals` 试过，立刻 170 条全是
>   噪声（这些页面本来就是「顶层函数 + 内联 onclick」的架构）。
> - **发行说明自动化**（G2）：正文改为从 git 历史生成。原来那段手写正文
>   还在讲 v1.0.1 的功能，而版本已经到 v1.4.x。
> - **CHANGELOG.md**（H1）：由 `tools/gen_changelog.py` 从提交信息生成，
>   并有测试盯着"不许落后于 git 历史"。
> - **基础镜像按 digest 固定**（G4）：同一个 commit 两次构建不再可能
>   拿到不同的基础镜像。
> - **容器日志上限**（G7）：`10MB × 3`，不再可能把磁盘写满。
> - **`/metrics`**（G9）：Prometheus 文本格式。刻意**不暴露**
>   `gt7_connected` —— PS5 关机是正常状态，做成 0/1 指标会让告警天天响。
> - 顺带修了 Windows 上 JSON 读写的瞬时占用：改成原子写 + 重试，
>   避免"刚存的设置看起来没生效"。
>
> 前两版（v1.4.0 / v1.4.1）补了三类驾驶事件、标定降级、赛道候选、阈值面板。

## ⬇️ 下载（Windows 免安装）

不想用 Docker 的话，可直接从 [Releases](https://github.com/DC1024/gt7-dash/releases)
下载 `gt7-dash-windows-x64.zip`：绿色免安装，含仪表盘 / 接收器 / 解密器三个 exe。
解压后先双击 `run-recorder.bat` 接收数据，再双击 `run-dashboard.bat` 打开
http://127.0.0.1:8787 查看。

## ✨ 特性

- **实时仪表盘**：转速表 / 速度 / 档位 / 踏板（含 20 秒踏板轨迹面积图）/
  G 力球（随 G 值摇晃）/ G-G 图（抓地力圆）/ 四轮温度 / 油量 / 涡轮 /
  圈速列表（分栏表：圈号 / 圈速 / 差值，BEST 高亮）
- **赛道地图（新）**：系统按轨迹的**几何指纹**自动识别赛道，在 `/tracks` 页把每条
  赛道画成形状图给你看——名字不对就改、改完保存或点「✓ 已记录」，同一条赛道的
  所有场次（含历史）立即统一改名；也可以用 `POST /api/v1/tracks/register`
  一键标注。识别结果存在 `data/tracks.json`（边车文件，跨部署/重装都不丢）
- **回收站可直接查看（新）**：回收站里的场次点「查看」就进详情页看摘要和轨迹，
  确认没用再彻底删除——不用像以前那样先恢复才能看
- **车型名片（新）**：历史场次列表与实时界面现在显示「品牌 + 车名 + 赛事组别徽章」，
  例如 `Porsche` 918 Spyder '13 `Gr.X`、Suzuki Escudo `Gr.X`。品牌来自
  ddm999/gt7info 的 `maker.csv`，组别（`Gr.1`~`Gr.4` / `Gr.B` / `Gr.X`）来自
  `cargrp.csv`，两张表都已内置；街车与未收录车自动不显示徽章
- **赛道 BoP 与官方 logo（新）**：`/tracks` 页给有标注的赛道加 **BoP 档位徽章**
  （高速 / 中速 / 低速，社区人工标注，仅部分赛道有）；并可选显示**官方赛道 logo**
  （跑 `python helper/download_track_logos.py ./data` 拉到 `data/tracklogos/`，
  图是游戏美术素材、**不随仓库分发**——没拉过时页面自动退回只画形状）
- **轮胎参考数据（新）**：新增 `GET /api/v1/tyres` 返回社区实测的**各胎种寿命圈数**
  与**湿地胎特性**（积水打滑阈值等）。🔴 这是参考资料不是遥测量——GT7 不下发胎种 /
  天气 / 积水率，不能用于实时判断，返回体里 `note` 也写了这句
- **行车轨迹双着色**：详情页的「行车轨迹」卡片可在「按踏板着色」与「按 G 力着色」
  之间自由切换（与实时仪表盘的轨迹同算法），圈号也能就地切换、不必刷新页面——
  踏板口径下刹车粉→红、油门青→绿，G 力口径按合成 G 大小铺蓝→绿→橙→红
- **圈间对比自选两圈**：参考圈与对比圈都可由下拉框指定，两圈圈长相差超过 2%
  时给出提示（曲线按距离对齐，长了的那圈尾部会被截掉）
- **图表点击放大**：所有图表点一下即全屏放大（canvas 按放大后的尺寸重绘、
  SVG 矢量缩放），Esc 或点空白关闭
- **逐帧遥测可查可导**：场次详情页展示全通道时序曲线（速度 / 转速 / 油门 / 刹车 /
  档位 / 横向纵向 G / 油量）、可翻页的逐帧原始数据表，并支持按整场或单圈导出 CSV
- **曲线十字光标读数**：鼠标划过逐帧曲线即出十字光标，点一下把读数钉住——
  该时刻的时间 / 速度 / 转速 / 油门 / 刹车 / 档位 / 横向纵向 G / 油量 / 圈号一目了然
- **分段计时与理论最快圈**：把一圈切成 N 段（缺省 4），逐段对比你的最好成绩，
  拼出「理论最快圈」，一眼看出哪一段在丢时间
- **轮胎滑移**：由四轮角速度自标定轮胎半径，检测空转 / 抱死，前后轴分开标定
- **走线偏差**：本圈相对参考圈的逐米横向偏移热力图，看你是走宽了还是切早了
- **驾驶事件时间线**：打滑 / 碰撞 / 极限刹车 / 轮胎滥用 / 大油门 / 出界，
  按圈展开
- **集锦高光**：事件按「置信度 × 类型权重」排序，给出可直接喂 ffmpeg 的
  `clip_start` / `duration`（含前后留白），做剪辑不用再自己找时间点
- **录像对齐**：把行车记录仪视频绑到遥测时间轴上（`offset_s` 或自动探测），
  之后按遥测时刻就能定位到视频的第几秒
- **进站与名次**：进站检测（油量环跳）/ stint 分析 / 实时名次时间线
- **每圈圈速**：自动记录本场每一圈，最快圈高亮
- **场次自动切分**：以「是否在赛道上」为边界，跑完回菜单自动结束，每场比赛独立归档
- **无损归档**：老场次自动压成 `.jsonl.gz`（无损，省 95% 空间），保留天数可调；
  也可以手动点「归档」。读的时候两种后缀都能直接打开
- **场次管理**：历史场次的收藏 / 改名 / 删除（移入 `_trash` 可找回）；
  场次默认按「车型 + 采集时间 + 最快圈速」模板命名（列表页可自定义模板）；
  回收站支持查看 / 恢复 / 彻底删除 / 一键清空，保留天数可调（0 = 永不自动清理）
- **可自定义布局**：卡片拖拽排序、隐藏/显示、半宽/整行，多套布局保存切换
- **个性化外观**：10 套主题预设（跟随系统 / 浅色 / 深色 / OLED 赛道黑 / 米黄护眼 /
  GT 竞速 / 冰川蓝 / HUD 荧光 / 午夜紫 / **座舱**）、强调色自选、卡片间距与读数大小、
  速度单位 km/h⇄mph、实时曲线窗口与线条开关、G 力量程，偏好存本浏览器并可导出/导入
- **公开 API**：`/api/v1/*` 共 **38 个端点**（实时 / 场次 / 逐帧与分析 / 赛道 /
  参考数据 / 回收站 / 设置 / 文档），返回解密后的结构化数据（带 CORS），第三方程序直接消费
- **数据本地化**：Salsa20 在本地解密，无云端、无第三方依赖

## 🚀 快速开始

PS5 → 设置 → 网络 → 打开 GT7 遥测上传功能，然后：

```bash
mkdir gt7-dash && cd gt7-dash
curl -O https://raw.githubusercontent.com/DC1024/gt7-dash/main/docker-compose.yml
# 🔴 先建 data 并交给容器用户（uid 10001）：
#    镜像里的 chmod 会被 bind mount 遮住，不授权的话首次启动写不了文件
#    （status.json 静默失败、录制器打不开 jsonl 直接退出）。
mkdir -p data && sudo chown 10001:10001 data
docker compose up -d
# 打开 http://<本机IP>:8787
```

> ⚠️ 两个服务都使用 `network_mode: host`——GT7 通过 UDP 广播发遥测，bridge 网络收不到。

## 🔌 数据 API

解密后的结构化数据通过 HTTP 公开（带 CORS），全部 **38 个端点**分组如下：

| 分组 | 端点 |
|---|---|
| 实时 | `/live`、`/laps` |
| 场次 | `/sessions`、`/sessions/<文件名>`、`/download`、`/favorite`、`/rename`、`/archive`、`/delete` |
| 逐帧与分析 | `/series`、`/frames`、`/csv`、`/raceline`、`/sectors`、`/slip`、`/deviation`、`/profile`、`/compare`、`/events`、`/pitstops`、`/highlights`、`/video` |
| 赛道 | `/tracks`、`/sessions/<文件名>/track`、`/tracks/<id>/rename`、`/tracks/<id>/confirm`、`/tracks/register` |
| 参考数据 | `/tyres` |
| 回收站与设置 | `/trash/purge`、`/trash/<文件名>/{restore,delete}`、`/settings`、`/settings/{trash-retention,name-template,archive-after}` |
| 文档 | `/docs` |

```bash
curl "http://<本机IP>:8787/api/v1/live?frames=120"        # 实时遥测
curl "http://<本机IP>:8787/api/v1/laps"                    # 每圈圈速
curl "http://<本机IP>:8787/api/v1/sessions"                # 历史场次
curl "http://<本机IP>:8787/api/v1/tracks"                  # 赛道库清单
curl "http://<本机IP>:8787/api/v1/sessions/<文件名>/profile" # 圈剖面（按赛道位置索引）
curl "http://<本机IP>:8787/api/v1/docs"                    # 文档本体（markdown）
```

给[赛道工程师](https://github.com/DC1024/gt7-coach)取数的几个接口：
`/profile` 返回按**赛道位置**（几何弧长，不是按时间）等分的剖面，并带
`meta.in_progress` 说明这一圈是否还在跑；场次列表每项带 `car_code`（数字车型码），
供跨车型时判断「是不是同一台车」。

完整字段与单位说明见 [`API文档.md`](API文档.md)，或直接访问 `/api/v1/docs`
（仪表盘右上角「API」按钮也能看到完整端点清单）。

## 🎧 赛道工程师（实时副驾）

仪表盘旁边可以再跑一个**赛道工程师** —— 它盯着同一份遥测，在该说话的时候
说一句，并且用浏览器的语音念出来（「1.5 秒后重刹区，参考最低 92」这种）。

它是独立仓库、独立进程：[`gt7-coach`](https://github.com/DC1024/gt7-coach)。
纯本地判断、零云依赖、纯标准库。

```bash
# 在**玩家电脑**上跑（扬声器旁边，不是服务器）
git clone https://github.com/DC1024/gt7-coach && cd gt7-coach
python -m gt7coach serve --dash http://192.168.43.18:8787
```

仪表盘实时页里会多出一张「赛道工程师」卡片：参考圈状态、本圈位置、
与参考圈的瞬时时间差、距下一个刹车点的距离/秒数，以及它刚说的话。
点右上角「语音：关」开启播报；地址不对时**点卡片上的提示就地改**（会记住）。

两边只通过 HTTP 联通：教练侧读本服务的公开 v1（`/api/v1/live` 拿实时帧、
`/api/v1/sessions/<f>/profile` 拿参考圈剖面），卡片侧读教练的
`/api/v1/coach/state`。拿不到教练服务时这张卡只显示「未启动」，
**不影响仪表盘本身**。

## 🗺️ 赛道地图（自动识别）

GT7 不下发赛道名，所以本服务**按轨迹形状自己认**：对某个场次的有效圈算
「几何指纹」（归一化折线，与平移 / 尺度 / 旋转 / 绕行方向无关），
互相之间 RMS 距离 ≤ 0.05 判为同一条赛道。

实测区分度很足：同一赛道不同场次的指纹距离约 0.006，最近的不同赛道约 0.24
—— 差 40 倍，所以阈值取 0.05 有充足余量。

- 打开 `/tracks`（顶栏「赛道地图」）看系统认出了哪些赛道，每条都画出形状图
- 名字不对就改：改完点「保存」，或名字本来对就点「✓ 已记录」
- 认领结果写进 `data/tracks.json`，**同赛道所有场次（含历史）一起生效**
- 没认出来的名字会标「自动识别·待确认」——那是按圈长猜的（候选来自
  [ddm999/gt7info](https://github.com/ddm999/gt7info) 的 course.csv，
  只含长度/弯道数、没有几何数据），圈长相近的不同赛道确实会猜错，
  所以以你确认为准
- 想批量补：用 `POST /api/v1/tracks/register` 一次标注（算指纹 → 挂靠 → 写名）

## 🧩 工作原理

```
PS5 (UDP :33740, 60Hz, Salsa20 加密)
  │
  ▼
gt7-recorder ── 心跳保活(5s) ──► PS5
  │  逐包解密 → 字段解析 → 场次判定 → jsonl 落盘
  ▼
data/status.json（最新帧 + 600帧环形缓冲 + 轨迹 + 圈速）
  │
  ▼
gt7-dashboard ── HTTP :8787 ──► 浏览器仪表盘 / 公开 API v1
```

关键实现细节（都写在代码注释里）：

- **解密**：Salsa20，key = `"Simulator Interface Packet GT7 ver 0.0"` 前 32 字节，
  nonce = `[iv ^ 0xDEADBEAF, iv]`（iv 在包偏移 0x40），
  解密后校验 magic `0x47375330`（小端 = `b"0S7G"`，不是 ASCII 直读！）
- **心跳保活**：每 5 秒向 PS5 发 1 字节 `"A"`，缺失会导致 PS5 停止发送遥测
- **场次切分**：以「离赛道持续 15 秒」为主边界（按墙上时钟检查，
  因为菜单里重复帧会被去重），圈数重置与包流超时作辅助
- **字段偏移**：与 [InvoGT] 的 `createTelemetryPacket` 逐字段对齐并实测验证

### 三种包格式能拿到什么

GT7 用**心跳字符**决定发哪种包（本项目 `--packet-type` 就是改这个字符）。
三者的差别不是"多几个字段"，而是**有没有某一整类数据**：

| 能力 | A 包（296B） | B 包（316B） | ~ 包（扩展） | C 包（368B，1.68+） |
|---|:---:|:---:|:---:|:---:|
| 速度 / 转速 / 档位 / 踏板 / 坐标 | ✅ | ✅ | ✅ | ✅ |
| 四轮温度 / 悬挂 / 轮速 | ✅ | ✅ | ✅ | ✅ |
| 名次 / 车数 / 官方圈速 | ✅ | ✅ | ✅ | ✅ |
| 车体三轴加速度（sway/heave/surge） | ❌ | ✅ | ❌ | ❌ |
| 方向盘转角 `steeringAngularVelocity` | ❌ | ✅ | ❌ | ❌ |
| 游戏滤波后的踏板（`throttleFiltered`） | ❌ | ❌ | ✅ | ❌ |
| 能量回收功率 | ❌ | ❌ | ✅ | ❌ |
| **前轮转角 `wheelSteeringAngle[2]`** | ❌ | ❌ | ❌ | **✅** |
| **轴距 `wheelBase`** | ❌ | ❌ | ❌ | **✅** |
| **每轮路面类型 `surfaceType[4]`** | ❌ | ❌ | ❌ | **✅** |
| **实时圈号 `currentLap`** | ❌ | ❌ | ❌ | **✅** |
| **赛事组别 `carCategory`** | ❌ | ❌ | ❌ | **✅** |
| 对手车辆的位置 / 速度 / 身份 | ❌ | ❌ | ❌ | ❌ |

> ⚠️ **本版只请求 A 包**（默认、最稳）。B / ~ 的解码已经写好了但**没有真机验证过**，
> C 包完全没支持。最后一行是真的协议限制：GT7 从不广播其他车辆的任何信息，
> 所以"某人在我前面 0.3 秒"这类数据拿不到，只能从名次变化间接推断。

## 📁 项目结构

```
gt7-recorder.py        遥测接收 / 解密 / 场次判定 / 落盘
gt7-dashboard.py       Web 仪表盘 + 公开 API v1（含赛道识别 /tracks 页）
gt7analysis.py         离线分析：圈速 / 分段 / 滑移 / 走线偏差 / 圈剖面 / 事件
gt7-decrypt/           Salsa20 解密器（Go，静态编译）
gt7-diagnose.py        收包诊断工具
gt7-event-detector.py  事件检测（打滑 / 急刹 / 超车 / 进站 / 最快圈…）
API文档.md             公开 API 完整文档（= GET /api/v1/docs）
docs/index.html        项目宣传页（GitHub Pages 源）
tests/                 pytest 回归测试（600+ 项）
tools/probe_fields.py  协议字段探针：协议里到底有没有转向角？
tools/probe_steer_angle.py  用真实记录验证「由横向 G 反算前轮转角」可信度
tools/check_attrs.py   结构静态检查（类属性引用校验）
```

## ❓ FAQ

**收不到数据？**
1. PS5 与服务器必须在同一网段，且 PS5 已开启 GT7 遥测上传
2. 仪表盘顶部「包格式」计数在涨但「采样」为 0 → 车辆未开动，跑起来即记录
3. 服务器防火墙放行 UDP 33739/33740

**暂停时数据不动？**
正常。GT7 暂停时物理冻结但仍以上报（速度 0/怠速），界面会显示「⏸ 游戏已暂停」。

**想锁定 PS5 的 IP？**
compose 里加 `--ps5 192.168.x.x`（默认 auto 自动发现）。

**赛道名字认错了 / 显示「自动识别·待确认」？**
正常——名字是按圈长猜的，圈长相近的不同赛道确实会猜错。去 `/tracks`
改成正确的名字点「保存」，同赛道所有场次会一起更正。形状图（指纹）本身
不会认错，认错的是名字。

**历史场次里的记录消失了？**
先看回收站（页面下方）；异常场次（没有完成圈 / 唯一圈 <20s 的菜单、停车场残片）
会在加载时自动移到回收站，保留期内可一键恢复。

**车型显示成 `CAR-ID-3656` 这种？**
那是社区车型表还没收录的车（数字码是 GT7 自己在遥测包里下发的，一定准；
缺的只是「码 → 名字」这一张表，来自 [ddm999/gt7info](https://github.com/ddm999/gt7info)）。
在数据目录放一张 `data/cars_extra.csv` 就能补上，格式和主表一样
`ID,ShortName,Maker`，**它优先于社区表**：

```csv
ID,ShortName,Maker
3656,Xiaomi Vision Gran Turismo,
```

改完重启服务即生效。仓库 `helper/cars_extra.csv` 是这份补充表的种子，
重跑下载脚本时若目标目录还没有补充表会自动带上它。

**想更新车型表？**
`python helper/download_cars_csv.py ./data`（重新拉社区最新表，
不会覆盖你已有的 `cars_extra.csv`）。
**为什么界面上没有方向盘转角？**

因为**本版只请求 A 包**，而 A 包（296B）里确实没有转向角。

转向角在 **C 包**（368B，GT7 **1.68 之后**才有）的 `wheelSteeringAngle[2]` 里。
换句话说这是**实现缺口，不是协议限制** —— 之前这一节写成"遥测包里没有这个字段"，
是错的，已更正。三种包格式的能力对照见上文
[「三种包格式能拿到什么」](#三种包格式能拿到什么)。

现在的替代方案：横向 G ÷ 速度² 反算等效前轮转角，精度不错（两条独立算法相关 0.98），
但打滑时失效，且轴距 / 转向比 GT7 不广播只能假设。详见 `tools/probe_steer_angle.py`。

支持 C 包已列入待办，需要一次真机抓包验证偏移（见项目状态盘点文档）。

## 🙏 致谢

- [ddm999/gt7info](https://github.com/ddm999/gt7info) — **车型 / 赛道 / 轮胎参考数据**。
  本项目用到它多张表：
  - `_data/db/cars.csv`（车辆 ID → 车名）：由 `helper/download_cars_csv.py`
    下载到 `data/cars.csv`，用来把遥测里的 `carCode` 显示成车名。
    自动更新地址：<https://ddm999.github.io/gt7info/data/db/cars.csv>
  - `_data/db/course.csv`（赛道 ID / 名称 / 长度 / 弯道数）：共 122 条，
    已内置进 `gt7analysis.py`，用于按圈长候选赛道名（内置而非运行时下载，
    是为了纯内网也能认赛道）。
  - `_data/db/maker.csv`（厂商 ID → 品牌 + 国别）、`_data/db/cargrp.csv`
    （车辆 ID → 赛事组别 `Gr.1`~`Gr.4`/`Gr.B`/`Gr.X`）：均已**内置**进
    `gt7analysis.py`，驱动车型名片里的品牌与组别徽章。
  - 赛道 BoP 档位（`course-bop` 社区标注）与官方赛道 logo（`img/track/*.png`，
    游戏素材、**不随仓库分发**，需 `helper/download_track_logos.py` 按需拉取）。
  - 轮胎寿命与湿地特性（`tyre-wear` / `wet-tyres` 社区实测），汇总进
    `GET /api/v1/tyres` 作为**参考值**（非遥测）。
- [InvoGT](https://github.com/InvolveDanny/InvoGT) — 字段偏移的权威参考
- [go-gt7-telemetry](https://github.com/snipem/go-gt7-telemetry) — 解密参数
- [Nenkai/gt7-udp](https://github.com/Nenkai) — 早期协议逆向

## 许可

[MIT](LICENSE)
