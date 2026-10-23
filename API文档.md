# GT7 遥测公开 API v1

给第三方程序读取**已解密、已解析**的 GT7 遥测数据。

## 读与写：两套不同的访问规则

| | 读接口（GET，绝大多数） | 写接口（POST，15 个） |
|---|---|---|
| 响应头 | 带 `Access-Control-Allow-Origin: *` | **不带 CORS 头** |
| 谁能调 | 任何页面 / 任何脚本 | **只有同源页面**（即仪表盘自己） |

**读接口**可以跨域调用：别的网站/本地工具直接 `fetch` 就能拿到数据。

**写接口**（改名、收藏、删除、恢复、改设置、阈值、归档…）**故意不给 CORS**。
原因不是懒：这些操作会改数据目录里的文件，而跨站写请求（表单 POST）
不需要预检就能发出去 —— 任何一个你打开的网页都能悄悄删掉你的场次。
浏览器对"读响应"有 CORS 限制，但对"发出写请求"没有，所以唯一可靠的
防线是**服务端不给跨域许可**。

实际影响：你在自己写的页面里调 `POST /api/v1/sessions/xxx/rename` 会被
浏览器拦下（控制台报 CORS 错）。这是**设计如此**，不是 bug。要在
自己的程序里做写操作，用非浏览器客户端（curl / Python / Node），
它们不受 CORS 约束。

- Base URL: `http://localhost:8787`（局域网内其他机器用 `http://<服务器IP>:8787`）
- 数据源：GT7 官方 UDP 遥测（33740 端口，Salsa20 本地解密），60 帧/秒
- 本文档也可通过 `GET /api/v1/docs` 获取（text/markdown）

## 接口一览

下面是**全部 v1 端点**（分组列出）。带 `<文件名>` 的路径里填
`GET /api/v1/sessions` 返回的场次文件名（归档过的填 `.jsonl.gz` 也认）。
逐帧 / 分析类接口的返回结构见下方同名章节。

### 实时

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/live?frames=N` | 实时遥测：最新一帧 + 最近 N 帧历史 + 轨迹 + G-G 散点 |
| GET | `/api/v1/laps` | 当前场次的每圈成绩与最快圈 |

### 场次列表与详情

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/sessions` | 历史场次文件列表 |
| GET | `/api/v1/sessions/<文件名>` | 指定场次的统计摘要 |
| GET | `/api/v1/sessions/<文件名>/download` | 下载原始 jsonl（attachment） |
| POST | `/api/v1/sessions/<文件名>/favorite` | 收藏/取消收藏，body `{"value": true}` |
| POST | `/api/v1/sessions/<文件名>/rename` | 改名，body `{"value": "新名称"}` |
| POST | `/api/v1/sessions/<文件名>/archive` | 手动归档：就地压成无损 `.jsonl.gz`（正在录制的场次返回 400） |
| POST | `/api/v1/sessions/<文件名>/delete` | 删除（移入服务器 `data/_trash/`，保留期后自动清除） |

### 逐帧与分析（每类的返回结构见下方同名章节）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/sessions/<文件名>/series?lap=N&max_points=2400` | 整场（或第 N 圈）降采样时序，用于画曲线 |
| GET | `/api/v1/sessions/<文件名>/frames?offset=0&limit=200&lap=N` | 分页逐帧数据（`limit` 上限 1000），用于表格 |
| GET | `/api/v1/sessions/<文件名>/csv?lap=N` | 全量 CSV 下载（带 UTF-8 BOM，Excel 直接打开不乱码） |
| GET | `/api/v1/sessions/<文件名>/raceline?lap=N` | 第 N 圈的行车轨迹（`lap` 缺省 = 最快圈） |
| GET | `/api/v1/sessions/<文件名>/sectors?n=4` | 分段计时 + 理论最快圈（`n` = 段数 2~10） |
| GET | `/api/v1/sessions/<文件名>/slip?max_points=120` | 轮胎滑移：空转 / 抱死检测 |
| GET | `/api/v1/sessions/<文件名>/deviation?ref_lap=&cmp_lap=&step=5` | 走线偏差：相对参考圈的逐米横向偏移 |
| GET | `/api/v1/sessions/<文件名>/profile?lap=N&step=5&prominence=12` | 一圈的按赛道位置索引剖面（刹车入点 / 弯心 / 给油点） |
| GET | `/api/v1/sessions/<文件名>/compare?ref_lap=N&cmp_lap=M` | 圈间对比数据（时间差曲线 + 关键点配对） |
| GET | `/api/v1/sessions/<文件名>/events?lap=N` | 驾驶事件时间线（打滑 / 碰撞 / 极限刹车 / 出界…） |
| GET | `/api/v1/sessions/<文件名>/pitstops` | 进站与名次：进站检测 / stint 分析 / 名次时间线 |
| GET | `/api/v1/sessions/<文件名>/highlights?top=10&pad_before=2&pad_after=1.5&min_score=0&types=&lap=` | 集锦剪辑时间轴（给出可喂 ffmpeg 的 `clip_start` / `duration`） |
| GET | `/api/v1/sessions/<文件名>/video` | 录像↔遥测锚点：读取当前绑定 |
| POST | `/api/v1/sessions/<文件名>/video` | 绑定录像 / 自动探测 / 解绑（`{clear:true}`） |

### 赛道识别

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/tracks` | 赛道库清单（id / name / ref_len_m / turns_cw / sessions 数 / verified / auto_named / `bop` / `logo`） |
| GET | `/api/v1/sessions/<文件名>/track` | 识别该场，返回 `{track_id, name, matched, distance, ref_len_m}`；首次会算指纹并落库 |
| POST | `/api/v1/tracks/<id>/rename` | body `{"value": "名"}`：赛道改名，同赛道所有场次一起生效 |
| POST | `/api/v1/tracks/<id>/confirm` | body `{}`：确认该赛道名正确，清掉「自动识别」标记 |
| POST | `/api/v1/tracks/register` | body `{"file": "<场次文件>", "name": "真实赛道名"}`：一键标注（补齐赛道库主入口） |
| GET | `/api/v1/tracks/<id>/ref-line` | 该赛道的**参考走线**（已生成的缓存）。没生成过返回 404 + 提示，**不会**现场计算 |
| POST | `/api/v1/tracks/<id>/ref-line` | body `{"max_sessions": 8}`（可选）：读该赛道历史场次、取每场最快圈、合成中位数走线并落盘。要几十秒 |

### 回收站与设置

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/trash/purge` | 清空回收站（彻底删除全部） |
| POST | `/api/v1/trash/<文件名>/restore` | 从回收站恢复场次到列表 |
| POST | `/api/v1/trash/<文件名>/delete` | 彻底删除回收站中的单个场次 |
| GET | `/api/v1/settings` | 回收站保留期/清单、归档保留期与场次命名模板 |
| POST | `/api/v1/settings/trash-retention` | 回收站保留天数，body `{"value": 30}`（0=永不清理） |
| POST | `/api/v1/settings/name-template` | 场次默认命名模板，body `{"value": "{车型} {时间} {最快圈}"}` |
| POST | `/api/v1/settings/archive-after` | 自动归档保留天数，body `{"value": 30}`（0=不自动归档） |
| POST | `/api/v1/settings/event-thresholds` | 事件阈值，body `{"values": {"spin_lat_g": 1.2}}`；值传 `null` 表示恢复该键默认。未知键 / 非数字返回 400 |

### 参考资料

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/tyres` | 轮胎参考数据：社区实测的各胎种寿命圈数 + 湿地胎特性（**非遥测量**，见下） |

### 文档

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/docs` | 本文档（text/markdown） |

> 另有 `/api/health`、`/api/state`、`/api/stats`、`/api/session` 是仪表盘页面
> 自己用的**内部接口**，不在 v1 承诺范围内（不保证兼容），这里不展开。
>
> 只有一个例外值得说明，因为自己写采集脚本时会用到 ——
> `GET /api/state` 支持三个查询参数：
>
> | 参数 | 默认 | 说明 |
> |---|---|---|
> | `frames` | 300 | 返回最近多少帧（夹在 10–600） |
> | `since` | 无 | 只返回序号 `q` 大于它的帧（增量），用于减少重复传输 |
> | `full` | 无 | `1` / `true` / `yes` 时**忽略 `since`**，强制返回整个窗口 |
>
> `full=1` 是给"刚从休眠唤醒、或网络断过一段，想立刻拿一份完整快照"
> 的场景准备的。不加它也能拿到全量（每 20 次写入会有一帧全量），
> 但那样得等一个周期；而消费方又没法自己算该等多久 —— `q` 是接收器
> 的序号，不是它自己的进度。

`favorite` 与 `custom_name` 会合并在 `GET /api/v1/sessions` 的返回里
（`favorite: bool`、`custom_name: string`），收藏的场次排在最前。
列表每项还带 `car_name`（车型短名，从 `cars.csv` 查 ShortName，
同目录的 `cars_extra.csv` 手工补充表优先，都查不到为 `CAR-ID-<码>`）。
`anomalous: bool` 表示异常场次（没有任何完成圈 / 唯一圈 <20s，多为菜单、停车场、
刚点火的残片）。这类场次在「历史场次」页加载时会**自动归档到下方回收站**（可恢复，
保留期内可一键恢复），因此主列表里通常看不到它们；回收站卡片内可勾选「只看异常场次」
单独筛出这些异常项。进行中的活场不会被误移（靠 status.json 的录制状态保护）。

### 参数

- `frames=N`：历史帧数，live 默认 120、上限 600；sessions 详情默认 0（不带回帧）
- `ref_lap=N`（sessions 详情 / 详情页）：指定参考圈号做行车轨迹/时间差对比分析，
  默认取最快圈；`cmp_lap=M`：指定被对比的圈，默认取最后一圈。
  圈号不存在或非有效（如手改 URL）时静默回退默认值。
- `cols=t,spd,rpm`（`/series` / `/frames` / `/csv`）：按需只取指定通道省流量；列名取自这些接口返回的 `cols` 字段。未知列名直接 400（附 `allowed` 列表），不静默少给几列；不传 = 全列（向后兼容）。
- 返回 404 的情形：场次文件名不存在 / 非法路径

## 字段与单位约定（对外承诺，只加不改）

### 顶层
| 字段 | 类型 | 说明 |
|---|---|---|
| `meta.api_version` | int | 接口版本，当前 1 |
| `meta.server_time` | float | 服务器 Unix 时间戳（秒） |
| `connected` | bool | 是否正在收到 PS5 遥测 |

### `session`
| 字段 | 类型 | 说明 |
|---|---|---|
| `frames` | int | 本场已落盘帧数 |
| `duration_s` | float | 本场时长（秒） |
| `max_speed_kph` | float | 本场极速（km/h） |
| `completed_laps` | int | 已完成圈数 |
| `lap_times` | array | `[[第几圈, 圈速毫秒], ...]` |
| `lap_started_at` | float | 当前圈起点的墙上时刻（秒，time.time()）。接收器每次冲线更新；仪表盘用它反推上一圈用时，作为 GT7 不报 last_lap 时的兜底圈速来源 |
| `car_code` | int | 车型码（全场非 0 众数；读不到为 0） |
| `car_name` | string | 车型短名，`cars.csv` 查 ShortName（`cars_extra.csv` 补充表优先），查不到为 `CAR-ID-<码>` |
| `car_maker` | string | 品牌名（内置厂商表）；查不到为空串 |
| `car_group` | string | 赛事组别 `Gr.1`~`Gr.4` / `Gr.B` / `Gr.X`；街车为空串 |
| `laps` | array | 逐圈详情，见下表 |
| `best_lap` | object 或 null | 最快圈 `{lap, time}`；无完整圈为 null |

`laps[]` 每项：
| 字段 | 类型 | 说明 |
|---|---|---|
| `lap` | int | 圈号 |
| `time` | float | 圈速（秒） |
| `incomplete` | bool | true = 该圈数据不足 20s，判为不完整（不计入 best_lap） |

> 圈速/赛道线分析统一走 `clean_laps` 剔除假圈：前圈（开局排队静止）、
> 末圈（完赛滑行离场）、菜单态（lap=65535），以圈距离偏离中位数判假。
> 列表与详情里的圈速表都不再出现这些假圈。

### `car`（速度/温度/开度等的单位都标在字段名里）
| 字段 | 单位 | 说明 |
|---|---|---|
| `speed_kph` | km/h | 车速 |
| `rpm` | rpm | 发动机转速 |
| `car_code` | int | 车型码（数字，不依赖车名表；0 = 未知） |
| `car_name` | string | 车型短名，`cars.csv` 查 ShortName（`cars_extra.csv` 补充表优先），查不到为 `CAR-ID-<码>` |
| `car_maker` | string | 品牌名（内置厂商表，如 `Porsche`）；查不到为空串 |
| `car_group` | string | 赛事组别：`Gr.1` / `Gr.2` / `Gr.3` / `Gr.4` / `Gr.B` / `Gr.X`（VGT、方程式、经典赛车）；**街车为空串** |
| `gear` / `suggested_gear` | int | 当前档 / 建议档，0 = 空挡 |
| `throttle` / `brake` | 0~1 | 踏板开度（乘 100 即百分比） |
| `position_m` | 米 | 车辆世界坐标 x/y/z（y 为高度） |
| `velocity_ms` | m/s | 速度矢量（模长 ≈ speed_kph/3.6） |
| `g_force.longitudinal` | g | 纵向：正值加速、负值刹车 |
| `g_force.lateral` | g | 横向：**正值左转、负值右转**（GT7 右手系，实测：正横向 G 时右侧轮更快）。注意 G 力球把正值画在**右边**（离心方向），所以左转时球往右漂 |
| `g_force.magnitude` | g | 合力大小 |
| `steer_angle_deg` | 度 | 前轮平均转角，**正 = 左转**。**仅格式 C 有**，A/B/~ 为 `null` |
| `wheel_base_m` | 米 | 轴距。**仅格式 C 有**，其余为 `null` |
| `car_category` | string | GT7 组别串（如 `GR1`）。**仅格式 C 有**，其余为 `null` |
| `surface` | string | 四轮路面材质，4 个字符（FL/FR/RL/RR）：`T`=铺装 `C`=路肩 `G`=草地 `s`=砂石/泥地（小写），如 `TCTC`。没见过的字符照原样返回。**仅格式 C 有**，其余为 `null` |
| `sway_ms2` | m/s² | 游戏直接给的**横向**加速度，**正 = 右转**。**仅格式 B/C 有** |
| `surge_ms2` | m/s² | 游戏直接给的**纵向**加速度，正 = 加速、负 = 刹车。**仅格式 B/C 有** |
| `fuel_pct` | 0~100 | 剩余油量百分比；**纯电车时是剩余电量 kWh**（看 `powertrain`） |
| `fuel_capacity_l` | 升 | 油箱容量（纯电为 0） |
| `powertrain` | 枚举 | 动力类型：`fuel`（燃油/混动）/ `electric`（纯电）/ `kart` |
| `energy_recovery` | kW 量级 | 能量回收功率；**仅扩展包(~)有值**，默认格式 A 恒为 0 |
| `max_energy_recovery` | kW 量级 | 本场能量回收峰值 |
| `throttle_filtered` | 0~1 | 游戏滤波后的油门输出（仅扩展包） |
| `brake_filtered` | 0~1 | 游戏滤波后的刹车输出（仅扩展包） |
| `turbo_boost` | bar 级 | 涡轮压力 |
| `engine` | 对象 | 引擎健康：`oil_pressure_bar` / `water_temp_c` / `oil_temp_c` / `body_height_m` |
| `shift_alert` | 对象 | 换挡提示：`min_rpm` / `max_rpm` / `shift_now`（转速已达换挡点） |
| `race` | 对象 | 比赛信息：`time_of_day_ms`（赛道时钟）/ `grid_position`（**当前名次**，0x84 在比赛中随排名实时变）/ `grid_start`（发车位，开跑瞬间快照；0=未捕获）/ `num_cars`（参赛车数） |
| `tyre_temp_c` | ℃ | 四轮表面温度，顺序 FL/FR/RL/RR |
| `suspension_height_m` | 米 | 四轮悬挂行程，顺序 FL/FR/RL/RR |
| `wheel_rad_per_s` | rad/s | 四轮**角速度**，顺序 FL/FR/RL/RR；记录器存的是绝对值，恒非负 |
| `wheel_rev_per_s` | rad/s | ⚠️ **已废弃**：名字写「转/秒」而实际单位是 rad/s，为兼容旧调用方保留，取值与 `wheel_rad_per_s` 完全相同 |
| `flags` | bit 位 | bit0 在赛道 / bit1 暂停 / bit2 加载 / bit3 在挡 … |
| `state.on_track` | bool | 是否在赛道上（比赛进行中） |

### `timing`
| 字段 | 单位 | 说明 |
|---|---|---|
| `current_lap` | int | 当前圈号 |
| `laps_in_race` | int | 本局总圈数 |
| `best_lap_ms` / `last_lap_ms` | 毫秒 | 最快圈 / 上一圈（null = 还没跑完） |
| `current_lap_time_s` | 秒 | **本圈**已用时（从冲线起算，冲线归零） |
| `current_lap_time_source` | str | `lap` = 真本圈用时；`session` = 兜底口径（退化成场次已用时）。只在老接收器没写 `lap_started_at` 时出现 `session` |

### `track`
| 字段 | 说明 |
|---|---|
| `path` | `[[x, z, 该点G值, 油门, 刹车, 圈号, 速度], ...]` 约 10Hz 采样的整车轨迹 |
| `gg_samples` | `[[横向g, 纵向g], ...]` 约 16Hz 采样的 G-G 散点 |

> `path` 的点位格式在 v1 内**向后兼容地加长**过：早期版本只有 `[x, z, G]` 三个值，
> 现在补到 7 个（多出的油门/刹车/圈号/速度用于画「行车轨迹」）。
> 消费方请按长度判断，缺字段时把油门/刹车当 0 处理，不要假设一定有 7 个。

### `history[]`（每帧一条）
`t`（服务器时间戳秒）、`speed_kph`、`rpm`、`gear`、`throttle`、`brake`、
`lap`、`tyre_temp_c`、`g_force`。

**格式 C 独有**（`v1.7.0` 起默认）：`steer_angle_l` / `steer_angle_r`
（左右前轮转角，弧度）、`wheel_base`（轴距，米）、`car_category`（组别，
字符串如 `"GR1"`）。A 包下这四个字段**不存在**（不是 null）——
缺字段和 0 值含义不同：转向角 0 是方向盘回正，没有转向角是包类型不支持。

> `lap` 的菜单态哨兵 `0xFFFF` 一律归一为 `0`（与 `series` / `frames` 同一口径），
> 所以历史窗里不会出现「第 65535 圈」。

## 历史场次的逐帧数据

场次 jsonl 里每帧有四十几个字段（速度/转速/档位/油门/刹车/G 力/四轮/油量…）。

### 包类型与新增字段

`v1.7.0` 起 recorder **默认请求格式 C**（368B，GT7 1.68+）。它包含格式 A 的
全部字段（实测逐项验证），并额外提供：

| 字段 | 单位 | 实测范围 | 说明 |
|---|---|---|---|
| `steer_angle_l` | 弧度 | ±0.17（约 ±10°） | 左前轮转角。与三点曲率反算的转向角幅度相关 r=0.789 |
| `steer_angle_r` | 弧度 | ±0.17 | 右前轮转角。与 `steer_angle_l` 互相关 r=0.965（Ackermann 差异） |
| `wheel_base` | 米 | 2.89~2.91 | 轴距。可用于把转向角换算成理论曲率 |
| `car_category` | 字符串 | `"GR1"` | GT7 的组别名，比查表更准 |

切回格式 A（GT7 < 1.68 需要）：
`GT7_PACKET_TYPE=A docker compose up -d --no-deps gt7-recorder`。

🔴 **包类型在「进入赛道」那一刻确定**，改完必须退出赛道重进才生效。
请求 C 却收到 A 时 recorder 会报警（日志 + 30 秒限流），不会静默录制。
下面三个接口把它按不同粒度暴露出来，供详情页与第三方分析使用。

| 接口 | 说明 |
|---|---|
| `GET /api/v1/sessions/<文件名>/series?lap=N&max_points=2400` | 整场（或第 N 圈）的**降采样**时序，用于画曲线 |
| `GET /api/v1/sessions/<文件名>/frames?offset=0&limit=200&lap=N` | **分页**逐帧数据（`limit` 上限 1000），用于表格 |
| `GET /api/v1/sessions/<文件名>/csv?lap=N` | 全量 CSV 下载（带 UTF-8 BOM，Excel 直接打开不乱码） |
| `GET /api/v1/sessions/<文件名>/raceline?lap=N` | 第 N 圈的**行车轨迹**（踏板 + G 力两套着色通道）；`lap` 缺省 = 最快圈 |
| `GET /api/v1/sessions/<文件名>/sectors?n=4` | **分段计时 + 理论最快圈**；`n` = 段数（2~10，缺省 4） |
| `GET /api/v1/sessions/<文件名>/slip?max_points=120` | **轮胎滑移**：空转 / 抱死检测；每圈曲线最多 `max_points` 点 |
| `GET /api/v1/sessions/<文件名>/deviation?ref_lap=&cmp_lap=&step=5` | **走线偏差**：本圈相对参考圈的逐米横向偏移热力图；`ref_lap` 缺省 = 最快圈，`cmp_lap` 缺省 = 最后一圈 |
| `GET /api/v1/sessions/<文件名>/events?lap=N` | **驾驶事件时间线**：打滑 / 碰撞 / 极限刹车 / 轮胎滥用 / 大油门 / 出界 / 压路肩；`lap` 缺省 = 全部圈 |
| `GET /api/v1/sessions/<文件名>/highlights?top=10&pad_before=2&pad_after=1.5&min_score=0&types=&lap=` | **集锦剪辑时间轴**：事件按 `置信度×类型权重` 排序，每段给出可直接喂 ffmpeg 的 `clip_start` / `duration`（含前后留白）。`top=0` = 不限；`types` 逗号分隔过滤 |
| `GET /api/v1/sessions/<文件名>/video` | **录像↔遥测锚点**：读取当前绑定（文件 / `offset_s` / 换算好的起止时刻） |
| `POST /api/v1/sessions/<文件名>/video` | **绑定录像**：body 给 `{file, offset_s}` 或 `{file, video_lead_s}` 或 `{file, video_start_iso}` 或 `{file, video_start_epoch}` 或 `{file, probe:true}`；`{clear:true}` 解绑 |
| `GET /api/v1/sessions/<文件名>/pitstops` | **进站与名次**：进站检测（油量环跳）/ stint 分析 / 实时名次时间线 |
| `GET /api/v1/sessions/<文件名>/compare?ref_lap=N&cmp_lap=M` | **圈间对比数据**（时间差曲线 + 关键点配对）；详情页切参考圈/对比圈时只取这一份（轻量，且 `race_line=0` 可再省 73% 流量），不刷新整页 |
| `GET /api/v1/sessions/<文件名>/profile?lap=N&step=5&prominence=12` | 一圈的**按赛道位置索引**剖面：几何折线 + 等距遥测 + 刹车入点 / 弯心 / 给油点。`lap` 缺省 = 最快圈 |

`series` / `frames` 返回的 `cols` 固定为
`["t", "spd", "rpm", "thr", "brk", "gear", "glat", "glon", "fuel", "lap"]`：

| 列 | 含义 | 单位 |
|---|---|---|
| `t` | 相对时间（整场=从场次开始；选圈=从该圈起点） | 秒 |
| `spd` | 速度 | km/h |
| `rpm` | 发动机转速 | rpm |
| `thr` / `brk` | 油门 / 刹车开度 | %（0~100） |
| `gear` | 档位 | — |
| `glat` / `glon` | 横向 / 纵向 G（沿用游戏内 `g_force` 的横向在前定义） | g |
| `fuel` | 油量百分比；纯电车无此口径时为 `null` | % |
| `lap` | 圈号；`0` 表示尚未进入计时圈（菜单态哨兵 `0xFFFF` 也归一为 `0`） | — |

`series` 额外返回 `laps[]`（每圈 `lap` / `t0` / `dur` / `frames`）、
`total_frames`（整场帧数）、`scope_frames`（当前范围帧数）、
`sampled_frames`、`step`（抽稀步长）。

## 分段计时与轮胎滑移

### 两套容差：为什么同一个"圈长"有两个不同的容忍度

代码里有**两个**圈长容差，数值差很多，用途完全不同。**不要把它们统一** ——
这是本文件存在的主要目的。

| 用途 | 容差 | 判什么 | 为什么是这个数 |
|---|---|---|---|
| **完整性**（`lap_is_complete`） | 基准圈长的 **90% ~ 125%** | 这一圈**是不是跑完了整条赛道** | 要能同时挡住残圈（实测 3.4%~61.2%）和时钟跳变假圈（149%~362%）。上下界落在实测分布的**空档**里（完整圈实测 93.4%~117%，117% 是滚动发车的出场圈），所以放到 ±10%/25% 也不会误伤 |
| **可比性**（`sector_times` 的 `dist_tol`） | 相对基准圈长的 **±3%** | 这两圈**能不能逐段对比** | 逐段对比要求段边界落在**同一个物理位置**。圈长差 3% 意味着同一条"直道"在两圈里差了 3% 的距离，段速已经不可比了。完整性容差宽是为了"别误杀"，这里窄是为了"别乱比" |

**两者混用会怎样**：

- 用 3% 判完整性 → 滚动发车的出场圈（117%）全被判成假圈，比赛第一圈直接消失
- 用 90%~125% 判可比性 → 一个 120% 的圈（跑错路线/多绕了一段）会被拿去和 100% 的圈逐段比，得出的"你在这段慢了 2 秒"是假的

**`sector_times` 里还有第二个容差**：`tol=0.05`（5%），判的是"哪些圈算接近最快圈"（用于算理论最快圈）。它和 `dist_tol` 不是一回事 ——
前者筛**时间**，后者筛**长度**。

### 其它容差一览

| 常量 | 值 | 作用 |
|---|---|---|
| `_LAP_GEO_GAP_M` / `_ARC_GAP_M` | 100 m | 单帧位移超过 100m（>3600 km/h）判为坐标跳变，不计入几何弧长 |
| `_DEV_START_GAP` | 60 m | 本圈起点与参考圈起点间距超过它 ⇒ 不是同一位置起步，不做走线对比 |
| `_DEV_RMS_TOL` | 60 m | 对齐后 RMS 偏移超过它 ⇒ 对齐明确失败，**不输出结论**（宁可没有，也不给错的） |
| `_FP_LEN_TOL` | 5% | 代表圈的圈长容差（相对中位数） |
| `_SLIP_SPIN_THR` / `_SLIP_LOCK_THR` | +10% / −15% | 后轴比车快 10% ⇒ 空转；前轴比车慢 15% ⇒ 抱死 |

> 事件检测的阈值（打滑 G、进站限速、名次稳定时长等）**不是**这些常量，
> 它们在运行时可通过 `POST /api/v1/settings/event-thresholds` 调整，
> 见「回收站与设置」一节。


这两张卡片回答的是「我还能快多少」和「我的胎是怎么被糟蹋的」。
两个接口都只依赖场次文件内容，服务端按场次记忆化，所以重复请求几乎零成本
（实测首次 0.39s / 0.53s，命中缓存后 4ms / 6ms）。

### `GET /api/v1/sessions/<文件名>/sectors?n=4` —— 分段计时 / 理论最快圈

每圈按**距离**等分成 `n` 段（不是按时间等分：只有把段边界钉在同一段路上，
跨圈的段用时才可比），插值出各段用时；每段取所有**可信圈**里的最快值求和，
就是「理论最快圈」。

| 字段 | 说明 |
|---|---|
| `n_sectors` / `tol` / `dist_tol` | 段数 / 圈速容差（0.05）/ 圈长容差（0.03） |
| `ref_dist_m` | 圈长中位数，圈长与它比对判断是否跑满整圈 |
| `actual_best_lap` / `actual_best_s` | 实际最快圈号 / 圈速 |
| `theoretical_best_s` | 理论最快圈（各段最快用时之和） |
| `potential_gain_s` / `potential_gain_pct` | 潜在空间 = 实际最快 − 理论最快 |
| `best_each_s[]` | 每段的场次最快用时 |
| `counted_laps[]` | 参与计算理论值的可信圈 |
| `partial_laps[]` | 圈长偏离中位数 >`dist_tol` 的圈，不给 `deltas` |
| `reliable` / `note` | 理论值是否可信；不可信时 `note` 写明原因 |
| `laps[]` | 逐圈 `{lap, total_s, dist_m, sectors[], counted, is_best, partial, deltas[]}` |

两条口径必须**先过滤再计算**，否则数字会骗人：

- **异常圈**：冲出赛道 / 进站 / 打转的圈里，某一小段可能"恰好很快"，把理论值
  拉到不真实地低。实测同一场 23 圈不过滤得到 `+8.48%` 的假空间，只留
  ≤最快圈×1.05 的 5 圈才是真实的 `+0.71%`。所以 `reliable` 只在可信圈
  ≥2 个时为 `true`，否则宁可空着也不给一个会骗人的数字。
- **残缺圈**：段边界按各圈**自己的**圈长等分，只有跑满整圈的圈边界才对得齐。
  实测第 1 圈只有 6129.6m（最快圈 6937.8m，录像从半圈处开始），它的第 1 段
  插出 25.2s，比全场最快的 33.2s 还"快" 8 秒 —— 纯属边界错位。更要紧的是
  圈长偏短的圈**总时长也偏短，完全可能被选成 `actual_best`**，所以这一步
  排在选最快圈**之前**，不只是从统计圈里剔掉。

> `theoretical_best_s < actual_best_s` 是**正常**的，不要当 bug「修」成相等。
> 残余偏差：同为完整圈时圈长仍有约 ±0.3% 的差（本次 5 个可信圈 6936~6960m），
> 段边界会错开十几米，理论值可能乐观 0.1~0.3s —— 所以 `dist_m` 要展示出来，
> 让人看得见可比性。

### `GET /api/v1/sessions/<文件名>/slip?max_points=120` —— 轮胎滑移

只有四轮角速度（`wheel_rads`）能反映「车轮实际转多快」，车身速度传感器
测不出空转和抱死。滑移率 `s = (ω·R − v) / v`：`s > 0` 轮子转得比车快
（空转），`s < 0` 转得比车慢（抱死）。

半径**自标定**，不依赖任何外部参数：自由滚动帧上 `ω·R ≈ v`，于是
`R = Σ(v·ω) / Σ(ω²)`（对 R 的最小二乘解）。**前后轴必须分别标定** ——
实测 R前 0.3391m / R后 0.3435m（比值 0.987）；若假设同半径，滑移率会被
整体偏置约 1.3%，而抱死的典型信号本身只有百分之几，偏置不可忽略。

| 字段 | 说明 |
|---|---|
| `available` | 缺 `wheel_rads` 的老场次为 `false`，前端据此整卡隐藏 |
| `calibration` | `{front_m, rear_m, ratio, free_frames, free_pct, ok, reason, free_slip_front, free_slip_rear}` |
| `laps[]` | 逐圈 `{lap, frames, front, rear, lockup_frames, wheelspin_frames}`；`front`/`rear` 为 `{min, p05, p50, p95, max}` |
| `lockup` / `wheelspin` | `{events, frames, worst[]}`；`worst[]` 最多 5 条 `{lap, t_rel, slip, speed_kph, throttle, brake, frames}` |
| `series` | 逐圈降采样曲线 `{t[], front[], rear[], speed_kph[], throttle[], brake[]}`，每圈 ≤`max_points` 点 |

- 事件按**连续帧**聚成一次（否则一次抱死会被算成 60 次），且至少 2 帧才算一次。
- 踏板要到位：抱死要求 `brake > 0.85`，空转要求 `throttle > 0.95`。
- `calibration.ok` 是自洽性检查：标定后自由滚动帧的滑移均值应接近 0
  （实测 −0.0001 / +0.0000）。偏得远说明标定被污染（自由滚动帧太少，
  或油门/刹车阈值没生效），此时 `reason` 写明原因。
- 自由滚动帧的噪声底 `|s| ≈ 0.0005`，而事件峰值从 `−1.000`（四轮全锁，
  ω 精确为 0）到 `+7.98`（低速全油门空转），阈值因此不敏感。
- ⚠️ 曲线是抽稀的，**1~2 帧的尖峰会漏掉**（抱死常常就这么短）——
  峰值一律从 `worst[]` 读，别从曲线读。

## 参考走线（不是「赛道中心线」）

**先澄清命名**：GT7 **不提供**赛道中心线，任何声称有中心线的工具都是自己算的。
这里提供的是「**你在这条赛道上历史最快圈的中位数走线**」—— 它反映**你自己的
跑法**，不是官方理想线。

这个区别不是抠字眼：如果你历史上一直在切某个弯，参考线就会鼓励你继续切。
把它当"标准答案"会强化坏习惯；把它当"我平时怎么跑"才有意义（用来发现
"今天这条线怎么偏了 3 米"）。

### 生成

```bash
POST /api/v1/tracks/1/ref-line
{"max_sessions": 8}        # 可选，默认 8，夹在 1~30
```

做的事：

1. 取该赛道挂靠的场次（新的优先，最多 `max_sessions` 场）；
2. 每场取**最快圈**（优先 GT7 官方 `last_lap_ms`，与列表/详情同一口径）；
3. 每圈按弧长等距重采样成 120 点（5km 赛道约每 42m 一个点）；
4. 先按圈长剔除偏离中位数 >3% 的走线（跑错路线/多绕一段）；
5. 剩下的**逐点取中位数**（不是平均 —— 一场冲出赛道就会把平均整体拉偏）。

返回：

| 字段 | 说明 |
|---|---|
| `pts` | 120 个 `[x, z]`，**原始世界坐标**（不是归一化指纹） |
| `dist_m` | 与 `pts` 一一对应的累计弧长（可直接喂给 `/deviation` 的 `ref_pts`） |
| `n_laps` / `n_used` | 参与统计的圈数 / 剔除离群后实际用上的圈数 |
| `len_m` | 参考线自身弧长 |
| `spread_m` / `spread_p95_m` | 各圈到参考线的**平均 / p95 横向距离**。1~3m 正常（不同圈本来就有差异）；**超过 10m 说明这些圈根本不是同一条走线**（多半是赛道识别错了） |
| `lap_len_cv` | 参与圈的圈长变异系数，万分之几是正常的 |
| `sources[]` | 每场的贡献：场次名、圈号、圈速、时间来源、走线长度、跳变次数 |
| `skipped[]` | 被跳过的场次及原因（解析失败 / 没有有效圈 / 坐标太少） |
| `source_note` | 固定文案，说明这不是官方中心线 |

### 读取

```bash
GET /api/v1/tracks/1/ref-line
```

**只读缓存**。没生成过返回 `404` 并提示用 POST 生成 —— 绝不会在 GET 里
现场计算：读 8 场 × 70MB 要几十秒，浏览器/脚本会以为服务挂了。

生成结果落在 `data/ref_lines.json`（**不塞进 `tracks.json`**：那是元数据，
改名/收藏时频繁读写，5KB 的线混进去会让每次改名都序列化几百 KB）。

### 一个实测样本

两条同赛道场次（圈速 102.384s / 101.830s）合成后：

```
参考线 5019.5m  120 点  用了 2/2 条
spread 1.07m (p95 2.49m)   圈长 CV 0.0004
用其中一条对它算偏差：rms 1.02m  coverage 1.00
```

两圈走线平均差 1 米、圈长几乎完全一致 —— 这个量级说明算法是可信的。

## 走线偏差

`GET /api/v1/sessions/<文件名>/deviation?ref_lap=&cmp_lap=&step=5`
—— 把「赛车线」「行进线」从「按 G + 踏板着色」升级成「按横向偏移量着色」。
本圈每一点投影到参考圈的折线上求垂足，dlat = 垂足到本圈点 在参考线法向上的投影；
前端按 dlat 标蓝 / 白 / 红，p95 截断色阶。回答的是「我在哪段路、开哪条线、偏了多远」。

| 字段 | 说明 |
|---|---|
| `ref_lap` / `cmp_lap` | 参考圈 / 对比圈；缺省 `ref_lap` = 最快圈、`cmp_lap` = 最后一圈 |
| `step` | 输出网格步长（米），1~50，缺省 5 |
| `ref_len_m` / `cur_len_m` | 参考圈 / 本圈的长度（米） |
| `coverage` | 本圈覆盖参考线的比例；< 0.6 ⇒ 残圈 ⇒ `reliable=false` |
| `reliable` / `reason` | 对齐是否可信；出场圈 / 残圈 / 距离对齐失败时不可比 |
| `rms_dlat` / `p95_abs_dlat` / `max_abs_dlat` | 偏移统计（米，p95 用于色阶截断） |
| `mean_dlat` | 整体偏离方向（带符号；坐标系手性决定正负，不要靠它判内侧外侧） |
| `inside_pct` | 弯心侧占比（按参考线自身曲率方向算）；直道 / 极贴线处为 `null` |
| `drift_m` | **诊断量**：本圈距离积分漂移（同一物理位置处本圈与参考线累计距离的差） |
| `start_arc_m` / `start_gap_m` / `end_gap_m` | 起点弧距、两圈起点物理间距、两圈终点物理间距；`start_gap_m > 60` ⇒ 出场圈 ⇒ 拒绝 |
| `ref_self_cross_m` | **诊断量**：参考线自交距离（沿赛道相隔 250m 的两点空间最近值）；仅展示，不作护栏 |
| `worst_win` | 10 段等弧长切分里 RMS 最大的那一段 `{from_m, to_m, rms_dlat, mean_dlat}` |
| `line[]` | 本圈走线重采样：`[x, z, dlat, inside]`；`inside ∈ {-1, 0, 1}` = {无效, 不在弯心侧, 在弯心侧} |
| `grid_m[]` | 与 `line[]` 一一对应的参考线弧长（米） |
| `ref_line[]` | 参考线等弧长采样 `[[x, z], ...]`（前端灰底图） |
| `segments[]` | 10 段等弧长切分：`{from_m, to_m, mean_dlat, max_abs_dlat, rms_dlat}` |
| `laps_list[]` | 本场所有有效圈号，给前端下拉用 |

- **几何对齐而不是距离对齐**：本圈每点投影到参考线折线上的最近线段。
  按距离对齐会被 `match_pv_pairs` 实测的 60~300 m 距离积分漂移污染
  （丢包时 `lap_samples` 跳过整段距离）；几何对齐天然免疫，
  沿赛道方向误差恒为 0，剩下的 dlat 才是真的横向差。
- **三道独立护栏，全过才给结论**：
  - `start_gap_m > 60` ⇒ 出场圈，跨圈回绕会让单调搜索锁错；
  - `coverage < 0.6` ⇒ 残圈（半圈起步 / 进站退出），不覆盖参考线；
  - `rms_dlat > 60` ⇒ 距离对齐明确失败，绝不吐出假横向偏移。
  三道都不过时返回 `reliable=false` + `reason`，但 `line` / `ref_line` 仍输出，
  前端照画灰底供肉眼参考，**RMS / 内侧占比这些数都别看**。
- **符号约定**：法向取参考线切向的 **+90° 旋转**（`N = (−Tz, Tx)`），
  所以「正 = 参考线行进方向的左侧」。但**别靠它判内侧外侧**——
  世界坐标的左右手性容易看反。要判内侧 / 外侧请用 `inside`
  （按参考线自身曲率方向算，不依赖坐标系手性）。

## 单圈行车轨迹

`GET /api/v1/sessions/<文件名>/raceline?lap=N` 只算**某一圈**的轨迹（缺省 `lap` = 最快圈，
该场没有有效圈时返回 `404` + `{"error":"no valid lap"}`）。返回：

| 字段 | 说明 |
|---|---|
| `lap` | 实际算的是第几圈 |
| `segments[]` | 按踏板通道切好的线段数组，每段 `{"color", "pts", "b", "t", "g"}` |

`pts` 是 `[[x, z], ...]`；`b` / `t` / `g` 与 `pts` **一一对齐**（长度相同），
分别是刹车开度 0~1、油门开度 0~1、合成 G 大小（`hypot(横向G, 纵向G)`）。
相邻两段首尾点重合（含各通道值），所以前端分多次 `stroke` 也不会有缺口。

> 分段是按**踏板**口径切的（`b`/`t` 判红/绿/滑行）。想按 G 力着色时，把同一条线的
> 所有点用 `g[]` 重上色即可——分段本身不影响 G 模式观感，只是多几次描边。

显式传入一个该场**不存在**的圈号不会报错，返回 `{"lap": N, "segments": []}`
（前端切圈时不必先探路）；只有「连最快圈都拿不到」才会 404。

## 圈间对比自选两圈

`GET /session?file=…&ref_lap=N&cmp_lap=M` —— `ref_lap` 是参考圈（缺省最快圈），
`cmp_lap` 是对比圈（缺省最后一圈）。两者都可手改，传入不存在或非有效的圈号会
**静默回退**到缺省值，不会把详情页打挂；两者撞成同一圈时自动换到另一圈。

⚠️ 时间差曲线是**按距离对齐**的，两圈圈长不同时曲线会截到短的那圈为止，因此
曲线末端的时间差**不等于**两圈圈速之差。详情页在两圈圈长相差 > 2% 时给出提示。

## 赛道自动识别

GT7 协议不下发赛道名（jsonl 表头 `circuit` 恒为 null），但轨迹形状稳定。
识别用**归一化形状指纹**：质心对齐 → RMS 半径归一 → 起点旋到 +X →
绕行方向统一为逆时针，再等弧长重采样 200 点。同一赛道两个场次的指纹
RMS 距离 ~0.006，最近的不同赛道 ~0.24（7 场实测，间隔 43 倍），
命中阈值取 `0.05`。

起点对齐**不需要旋转搜索**：同赛道车永远从同一物理位置过线。
反向跑的圈会在「镜像成逆时针」一步被拉齐——形状相同就命中同一条。

| 接口 | 说明 |
|---|---|
| `GET /api/v1/tracks` | 赛道库清单（id / name / ref_len_m / turns_cw / sessions 数 / verified / auto_named / `bop` / `logo`） |
| `GET /api/v1/sessions/<文件名>/track` | 识别该场：返回 `{track_id, name, matched, distance, ref_len_m}`；首次会算指纹并落库 |
| `POST /api/v1/tracks/<id>/rename` `{"value": "名"}` | 赛道改名，同赛道所有场次一起生效 |
| `POST /api/v1/tracks/<id>/confirm` `{}` | 用户确认该赛道名正确：清掉 auto_named 标记，UI 不再提示待确认 |
| `POST /api/v1/tracks/<id>/restore` `{}` | 恢复「已归档」赛道：清掉 archived 标记，回到主网格 |
| `POST /api/v1/tracks/<id>/delete` `{}` | 彻底删除赛道（不可恢复）；仍挂着场次时返回 409 |
| `GET /tracks` | 赛道库管理页（Web）：查看系统识别的赛道形状 + 名称，可改名、确认 |
| `GET /trash-view?file=<名>` | 回收站场次查看页（Web）：摘要 + 轨迹缩略图，便于判断要不要删除 |
| `POST /api/v1/tracks/register` `{"file": "<场次文件>", "name": "真实赛道名"}` | 一键标注（补齐赛道库主入口）：算指纹→挂靠/新建→写名，同赛道所有场次一起生效 |

- 代表圈挑选：圈长（坐标折线长度）相对**中位数** ±5% 先剔脏圈/残圈，
  幸存圈里挑用时最短的——最快圈走线最干净。
- 库存 `data/tracks.json`（边车文件，jsonl 不可变）。`tracks[].name` 为空
  表示未命名，详情页显示「未命名赛道 #N」并给 ✎ 改名入口。
- 场次→赛道映射也在这个文件里：**详情页首次打开时识别并落库**，
  列表页只读映射展示赛道名，绝不触发识别本身（那要解析整场 jsonl）。
- 映射带场次文件的 `sig`（mtime+size）快照：文件继续写入后快照失配，
  下次访问自动重算指纹——录制中途被扫到而建的「半圈赛道」会在跑完后
  自愈并回真赛道，不再永久错分。
- **识别只对「已跑完」的场次做**（无完成圈 / 唯一圈 <20s / 正在录制的活场
  一律不进识别队列），从源头杜绝拿半圈指纹建赛道。
- 一场都不挂、也未被你确认过的赛道会**自动进「已归档」区**
  （`tracks[].archived=true`，含 `archived_at`/`archived_reason`），
  语义同场次回收站：赛道页底部折叠展示，可恢复、可彻底删除。
  `GET /api/v1/tracks` 每项带 `archived` / `archived_at` 字段。
- 无有效圈的场次返回 `{error}`，不落库、不影响页面。

### 赛道 BoP 与官方 logo（附加字段，可为空）

`/api/v1/tracks` 每条赛道多给两个**参考字段**，来源同样是 ddm999/gt7info：

| 字段 | 取值 | 说明 |
|---|---|---|
| `bop` | `""` / `"High"` / `"Mid"` / `"Low"` | 该赛道在 GT7 线上赛启用的性能平衡档位（高速/中速/低速）。**只有 91/122 条赛道有标注**，其余为空串。它是社区人工标注，且官方后来不再公布各档具体数值，所以仅供展示，不要参与计算 |
| `logo` | 如 `"suzuka"` | 官方赛道 logo 的**文件名主干**。有图时图片地址是 `/tracklogos/<logo>.png`（例：`/tracklogos/suzuka.png`）。图是**可选素材**——要用就跑 `python helper/download_track_logos.py` 拉到 `data/tracklogos/`，没拉过该路径返回 404，页面自动退回只画形状 |

🔴 logo 是游戏美术素材，**不随代码仓库分发**（版权），仓库里只有「赛道 ID → 文件名」的映射。

## 轮胎参考数据

`GET /api/v1/tyres` —— 社区实测的胎寿命与湿地胎特性。

🔴 **这是参考资料，不是遥测量。** GT7 的 UDP 遥测**不下发**胎种、天气、
赛道积水率，所以下面这些数字**不能**用来做实时判断（比如"你的胎还剩 37%"
这种说法一定是编的）。返回体里 `note` 字段也写了这句话，它会跟着数据一起走。

返回 `{life[], wet[], source, note}`：

`life[]` —— 各胎种跑到失效的圈数（50 倍胎耗下的两组样本，左右前轮分开记）：

| 字段 | 说明 |
|---|---|
| `compound` | 胎种：`RH`/`RM`/`RS`/`IM`/`W` |
| `car` / `track` / `wear_rate` | 样本条件（车 / 赛道 / 胎耗倍率） |
| `laps_front_right` / `laps_front_left` | 前轮寿命圈数；只记了一侧时另一个是 `null` |
| `note` | 备注 |

`wet[]` —— 湿地各胎种特性：

| 字段 | 说明 |
|---|---|
| `compound` | 胎种 |
| `ref_lap` | 同一条湿地赛道的参考圈速（`m:ss.mmm`），可横向比较 |
| `aquaplaning_pct` | 开始打滑的**赛道积水率**（%） |
| `note` | 定性描述 |

> 实用结论：`Intermediate` 在积水 <50% 时与 Sports Soft 同速且能保持，
> 是湿地最稳的选择；`Racing Hard` 只在积水 <30% 时才比 Sports Soft 快。

## 驾驶事件时间线

`GET /api/v1/sessions/<文件名>/events?lap=N` —— 从遥测里检测驾驶事件，
给复盘提供时间锚点。检测引擎是仓库里的 `gt7-event-detector.py`
（dashboard 用 importlib 就地加载；文件被裁剪掉时返回
`{"available": false, "reason": …}`，前端整卡隐藏，不报 500）。

| 字段 | 说明 |
|---|---|
| `available` | 检测器文件缺失 / 无帧时为 `false`，`reason` 写明原因 |
| `calibration` | 前后轴标定半径（与 `/slip` 共用同一次标定）；`available=false` 时滑移用兜底半径 |
| `events[]` | `{lap, t_rel, t_end_rel, type, confidence, evidence, hint, src}`，按 (圈, 圈内秒) 排序 |
| `type` | `spin`（打滑/失控）/ `collision`（碰撞）/ `hard_braking`（极限刹车）/ `tyre_abuse`（轮胎滥用）/ `heavy_throttle`（大油门出弯）/ `off_track`（出界）/ `overtake`（超车·被超）/ `pit_like`（进站）/ `lap_record`（最快圈）/ `tank_slap`（压路肩，需格式 C） |
| `t_rel` / `t_end_rel` | 事件起止相对**该圈起点**的秒数（与 `/series?lap=N` 同一时间基准） |
| `src` | 事件来源：`telemetry`（检测器）/ `geometry`（走线偏差出界）/ `slip`（出界的滑移兜底）/ `lap_time`（官方圈速，用于 `lap_record`） |
| `laps` / `lap_scope` | 本次覆盖的圈号清单 / `?lap=N` 的筛选值（0 = 全场） |
| `thresholds` | 本次检测用的全部阈值（透明化，别猜） |

- 圈口径与圈速表 / 赛车线**共用 `clean_laps`**——检测器自带的
  `find_lap_boundaries` 不剔假圈，菜单态 / 前圈不会混进来。
- 滑移率用前后轴分标定半径（`calibrate_wheel_radii`，与 `/slip` 同一次
  记忆化结果）；带符号定义与 `/slip` 一致：正 = 空转，负 = 抱死。
- **`off_track` 由走线偏差主导**：本圈相对参考圈 `|dlat| > 5m` 的持续段
  （最短 8m）判出界。压草地未必滑移大、高速冲出弯也出界——检测器自带的
  「低速 + 高滑移」判据只在偏差不可信时兜底（此时 `src=slip`）。
- 🔴 结果按场次记忆化：**冷路径 ~10s**（逐帧构造 21 万个 Sample 逐圈跑
  检测），所以事件卡**展开时才取**；同场次再取 0ms。别把它塞进详情页
  首屏串行链路。
- `tyre_abuse` 的 `evidence` 带 `四轮胎温_C`（1 位小数）与 `四轮悬挂_mm`
  （整数）——**都是真值**（2026-10-09 起胎温 / 悬挂进列式存储；实测两场真实
  jsonl 胎温 54~97℃、悬挂 0~0.3m）。
  🔴 胎温**必须保留小数**：四轮差异常常只有零点几度，取整会抹成
  `[60,60,60,60]`，而这个事件的判据恰恰就是「四轮不一样」。
- 🔴 `tyre_press` / `tyre_wear` 在格式 A 下**恒为 0**（GT7 不广播），
  因此既不存也不进证据——免得解说词里出现假数字。
- 🔴 **协议里没有方向盘角度**（2026-10-09 探针结论）。`spin` / `off_track`
  证据里的 `方向`（左 / 右）和 `过弯半径_m` 是用**曲率**换算的，不是协议字段：
  `κ = 横向G × 9.80665 / v²`，与轨迹几何曲率相关 0.986（误差中位 0.0004 1/m），
  符号用「外侧轮角速度更高」独立验证过（横向 G 为正 = 左转）。
  想复核 / 找真字段：`python tools/probe_fields.py --selftest`（自证工具）
  然后开车时 `--ps5 <IP> --seconds 25` 跑一次。

## 集锦高光（剪辑时间轴）

`GET /api/v1/sessions/<文件名>/highlights` —— 把上面的事件排成**可直接切片**
的集锦时间轴，是 POV 解说工具「自动剪集锦」的交接面。

| 字段 | 说明 |
|---|---|
| `session_start` / `session_start_iso` | 场次起点（墙上时钟 / 本地时间 ISO）——录像对齐的锚点 |
| `clips[]` | `{rank, score, type, type_cn, lap, confidence, t_start, t_end, clip_start, clip_end, duration, evidence, hint}` |
| `score` | `confidence × 类型权重`；权重随 `weights` 一起返回（碰撞 10 > 打滑 8 > 出界 7 > 轮胎滥用 5 > 大油门 4 > 极限刹车 3） |
| `t_start` / `t_end` | 事件本身，相对**场次起点**的秒数 |
| `clip_start` / `clip_end` / `duration` | 含前后留白的切片窗口：`ffmpeg -ss <clip_start> -i video.mp4 -t <duration>` |
| 参数 | `top`（0=不限，缺省 10）/ `pad_before`（2.0）/ `pad_after`（1.5）/ `min_score` / `types`（逗号分隔）/ `lap` |

- 🔴 切片窗口**必须带留白**：从事件正中间开始切，观众看不到"怎么发生的"。
- 事件只有 `t_rel`（相对圈起点），`/highlights` 负责把它还原成整场时间轴。

## 录像对齐（录像 ↔ 遥测 时间轴锚点）

录像和遥测是**两条独立的时间轴**，中间差一个只有玩家知道的常数（什么时候按的录制）。
不把这个常数存下来，`/highlights` 给的秒数就没法直接喂 ffmpeg —— 所以它是剪辑 / 配音的
前置条件。

🔴 **口径（三行一起看，弄反一个整段剪辑就错位）**

```
t_session = 相对「本场第一个有效圈起点」的秒数   ← /highlights 给的就是它
offset_s  = 录像开始时刻 − session_start          ← 负数 = 录像比遥测早开始
t_video   = t_session − offset_s                  ← ffmpeg -ss 要的是这个
```

例：开跑前 18 秒就按了录制 → `offset_s = −18` → 遥测第 30s 对应录像第 48s。
怕正负号搞反就用 `video_lead_s`（= −`offset_s`，"录像早开始 18 秒"填 18）。

| 字段 | 说明 |
|---|---|
| `bound` | 是否已绑定 |
| `file` | 录像文件路径（只是记下来，服务端不打开它） |
| `offset_s` / `video_lead_s` | 偏移 / 反向的直觉读法 |
| `session_start` / `session_start_iso` | 遥测时间轴零点（本地时区） |
| `video_start_epoch` / `video_start_iso` | 换算出的录像开始时刻 |
| `source` | `manual` / `ffprobe` / `mtime`——怎么定出来的 |

绑定后 `/highlights` 会多给一份录像时间轴：

- `video`：上面的绑定信息
- `clips[].clip_start_video` / `clip_end_video`：这段高光在**录像**里的秒数
- `ffmpeg_hint` 会换成带真实文件名的版本

存哪儿：`data/sessions_meta.json`（边车，与收藏 / 改名同一先例 —— jsonl 是不可变原始数据）。

🔴 **自动探测的坑**：`{"probe": true}` 只在录像文件**就在跑 dashboard 的这台机器上**时有效
（Docker 部署时看不到你电脑的文件）。有 `ffprobe` 会用 `mtime − duration` 反推开始时刻；
没有就只能用 mtime，而**多数录制软件（OBS / PS5 相册 / 采集卡）的 mtime 是"录完"的时刻**，
会偏晚整整一段录像的时长 —— 这种情况响应里会带 `warning`，别当准确值用。

## 进站与名次

`GET /api/v1/sessions/<文件名>/pitstops` —— 进站检测 / stint 分析 /
实时名次时间线，三块一次算完（全场帧遍历一遍 ~0.3s，结果记忆化）。

| 字段 | 说明 |
|---|---|
| `powertrain` | `fuel` / `electric`（按全场 `gas_capacity` 众数判定；电车 `gas_capacity==0`） |
| `pitstops[]` | `{lap(出站圈), prev_lap, t_rel(相对出站圈起点), before, after}`；判据 = 油量比上一帧高 >5 |
| `stints[]` | 进站切开的跑段：`{stint, from_lap, to_lap, laps, dur_s, gas_start, gas_end, fuel_used, fuel_per_lap, after_stop}` |
| `positions[]` | 每圈末名次 `{lap, pos}`（画时间线的骨架） |
| `pos_events[]` | 名次逐帧变化：`{lap, pos, t_rel, from, delta}`；`delta<0` 超车、`>0` 被超；首个事件无 `from` |
| `laps_list[]` / `lap_durs{}` | 有效圈号清单 / 每圈时长（秒） |

- 🔴 **进站判据是油量环跳**：GT7 油量只会单调消耗（进站加油才会大幅上升）。
  遍历范围是 `clean_laps` 的有效圈——菜单态/离场段的「油量重置回满」
  （场次结束后 gas 回 100）**不会**被误判成进站。电车**不检测**——进站不
  加油，电量回升可能来自再生回充，环跳判据不成立。
- 🔴 **名次来自 `quali_pos`(0x84)**：比赛进行中它是**当前名次**（逐帧实时变，
  实测一场 1~20 名全出现、49 次变化），不是排位成绩；0/65535 是菜单态哨兵。
  真正的发车位在接收器开跑瞬间的快照 `grid_start` 里。
- 🔴 **轮胎磨损广播协议里没有**（296 字节包无 wear 字段），这张卡**不含换胎
  判定**——轮胎寿命请看游戏内 HUD 自行判断。胎温（`tyre_temp`）协议里有，
  但列式存储未存、且温度 ≠ 磨损，不要拿它当磨损用。

## 圈剖面（给赛道工程师 / 第三方按位置索引一圈）

`GET /api/v1/sessions/<文件名>/profile?lap=N&step=5&prominence=12`

赛道工程师（gt7-coach）要的是「一圈的几何折线 + 等距遥测 + 刹车点/弯心/给油点」。
这些东西本服务里**都已经有了**（`clean_laps` / `lap_samples` / `find_peaks_valleys` /
距离重采样），消费方自己重写一遍不仅费力，还会因为实时侧只有 10Hz 轮询而精度更差、
第一圈完全没有参考。所以一次性打包发出去，谁都不用重复建图。

### 🔴 距离轴是几何弧长，不是速度积分

`/series`、`/sectors`、`/deviation` 用的是 `dist += v·Δt` 积分，**一圈漂移 60~300 m**。
拿它做「本圈 1200 m vs 参考圈 1200 m」对齐，实际赛道上能差十几米 —— 实时刹车点
预告会直接指错位置。`/profile` 改用 `car_x`/`car_z` 相邻弦长累积的**几何弧长**，
只依赖坐标，与速度无关，不漂移。两个口径都返回：

| 字段 | 口径 |
|---|---|
| `length_m` | 几何弧长（本接口的距离轴） |
| `length_by_speed_m` | 速度积分（老口径，留作对照） |
| `length_drift_pct` | 两者相差百分比；绝对值超过 3% 会在 `warnings` 里明说 |

### 返回结构

| 字段 | 说明 |
|---|---|
| `grid_m` | 等距网格（步长 `step`，米） |
| `speed_kph` / `throttle` / `brake` / `t_rel_s` / `glat` / `glon` | 与 `grid_m` **一一对齐**的通道 |
| `pt.x` / `pt.z` | 同一网格上的几何折线，消费方用它做实时最近点定位；本圈无坐标时为空数组 |
| `markers.brake_in[]` | 刹车入点：`s_in_m` / `s_out_m` / `speed_in_kph` / `peak_brake` / `duration_s` / `v_min_kph` |
| `markers.apex[]` | 弯心：`s_m` / `speed_kph` / `glat` / `radius_m` / `turn`（左/右）。半径由 `κ = G_lat·g/v²` 反推 —— 格式 A/B/~ **没有转向角**（格式 C 从 v1.7.0 起有 `steer_angle_l/r`，但本接口仍用曲率，好让 C 包场次与历史 A 包场次口径一致） |
| `markers.throttle_on[]` | 出弯给油点：`s_m` / `speed_kph` / `after_apex_m` |
| `markers.peak[]` / `markers.valley[]` | 原始速度极值点（峰/谷交替） |
| `warnings[]` | 降级与口径提示。**空数组才是干净的**，非空必须往界面上显 |

`step` 缺省 5 m（clamp 到 1~100），`prominence` 缺省 12 km/h（clamp 到 1~60，
峰谷显著度阈值）。点太多（>3000）会自动放大步长，实际值回填在 `step_m`。

圈长不足 200 m、或帧数不足 20、或该圈不在有效圈里时返回 `{"error": ...}`
（额外给 `why` 与 `available_laps`），**不返回半成品关键点**。

### 🔴 正在跑的那一圈不能当参考圈

一场正在录制的比赛里，圈号最大的那一圈还在跑：它时长更短（在 `clean_laps`
的速度积分口径下反而"最快"）、坐标折线只覆盖半条赛道。拿它当参考，实时最近点
定位会大面积失配 —— 实测横向误差飙到 **300 m**（≈ 车在 5 秒里跑过的距离），
而表现形式是"偶尔算错"而不是报错。

所以录制进行中时，本接口会排除**原始帧里圈号最大的那一圈**（判据取原始帧的
最大圈号，不是 `clean_laps` 过滤后的 —— 半圈常已被当假圈剔掉，那时取过滤后的
最大值会误伤一个跑完了的合法圈）。`meta.recording` 会说明当前是否录制中，
`why` 为 `lap_in_progress` 时表示请求的正是那一圈，`no_lap` 表示一圈都还没跑完。

**⚠️ 有一个例外必须由调用方自己兜住：本场只有一圈时。** 那时上面那条排除
规则会把唯一的一圈也排掉（等于永远没有参考圈可用），所以接口会**退回把这一圈
（半圈）发出去**。而这一刻 `recording` / `available_laps` 与"已经跑完一圈"时
**长得一模一样** —— 光看"有没有拿到 profile"分不出来。所以响应里明说了：

| 字段 | 含义 |
| --- | --- |
| `meta.in_progress` | **返回给你的这一圈还在跑**（只有上面那个例外才为 `true`）。消费方必须据此拒绝它 |
| `meta.in_progress_lap` | 原始帧里圈号最大的那一圈（= 正在跑的那一圈；没在录制时为 `null`） |

`s_m` 那套最近点定位对这个字段特别敏感：半圈的折线只覆盖半条赛道、圈长还随车
前进一直变（实测一场里 5491 m 一路涨到 7493 m），拿它定位会得到几十米的横向
误差（表现为"出界了"和指错位置的刹车预告），**而且只在开局那几十秒出现**，
极难复现。

## 使用示例

```bash
# 实时数据（最近 60 帧）
curl "http://localhost:8787/api/v1/live?frames=60"

# 只要圈速
curl "http://localhost:8787/api/v1/laps"

# 历史场次列表，然后取某场的统计
curl "http://localhost:8787/api/v1/sessions"
curl "http://localhost:8787/api/v1/sessions/20261007_045628_unknown_6ac5607c.jsonl"

# 详情页分析（行车轨迹 / 时间差对比）；?ref_lap=N 参考圈（缺省=最快圈）、?cmp_lap=M 对比圈（缺省=最后一圈）
curl "http://localhost:8787/session?file=20261007_045628_unknown_6ac5607c.jsonl"
curl "http://localhost:8787/session?file=20261007_045628_unknown_6ac5607c.jsonl&ref_lap=3"
curl "http://localhost:8787/session?file=20261007_045628_unknown_6ac5607c.jsonl&ref_lap=3&cmp_lap=7"

# 单圈行车轨迹（踏板 + G 力两套着色通道）
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/raceline?lap=3"

# 走线偏差（参考圈 vs 对比圈，逐米横向偏移）
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/deviation"
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/deviation?ref_lap=6&cmp_lap=7&step=5"

# 赛道自动识别（首次识别并落库，之后走缓存）
curl "http://localhost:8787/api/v1/tracks"
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/track"

# 逐帧数据：整场时序 / 第 3 圈时序 / 翻页 / 导出
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/series"
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/series?lap=3"
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/frames?offset=200&limit=200"
curl -o lap3.csv "http://localhost:8787/api/v1/sessions/SESSION.jsonl/csv?lap=3"
```

## 稳定性说明

- v1 字段**只加不改名不改单位**；将来不兼容的改动会升到 v2 并保留 v1
- `history` 的条数上限 600；`path`/`gg` 上限 4000/1200 点
- `frames` 的 `limit` 上限 1000；`series` 的 `max_points` 上限 6000
- 服务器单线程 HTTP，请勿高频轮询（≥100ms 间隔为宜）

## 传输与归档

### 响应压缩与连接复用
- **gzip 压缩**：客户端带 `Accept-Encoding: gzip` 时，正文（JSON / HTML / 页面）自动压缩（level 6）；附件类下载（CSV、`/download`）**不压缩**，避免浏览器把文件存成 `.gz`。可用 `--no-gzip` 启动参数关掉（排障 / 抓包看明文）。
- **HTTP/1.1 keep-alive**：默认连接复用；客户端可发 `Connection: close` 显式关闭。多请求场景（如详情页并行取数）较无 keep-alive 提速约 10×。

### 圈间对比轻量接口 `/compare`
`GET /api/v1/sessions/<文件名>/compare?ref_lap=N&cmp_lap=M`（可加 `&race_line=0`）返回 `compare_session` 同款结构，**不含**占 73% 体积的 `race_line`（行车轨迹卡会自己另行取 `/raceline`）。详情页切参考圈 / 对比圈时只取这一份并重画依赖它的三块，不刷新整页（整页重来约 196KB / 720ms，XHR 约 8KB / 590ms），且不闪屏。

### 存储归档（无损）
- 老场次自动 gzip 压缩为 `<名>.jsonl.gz`（体积约 1/8~1/10，**无损、字节可还原**），后台静默进行，不阻塞列表页；正在录制（文件近 20s 内有写入）的活场不归档。
- `GET /api/v1/sessions` 每条带 `archived: bool` 字段标明是否已压缩。
- 自动归档保留期：`POST /api/v1/settings/archive-after {"value": 30}`（天，0=不自动归档，上限 3650）。
- 访问归档场次无需改 URL：`/session`、`/api/v1/sessions/<名>`、`/series`、`/csv` 等都会用 `resolve_session` 自动兜底成 `.jsonl.gz`；`/download` 与 CSV 导出会自动带 `.gz` 后缀。
- 收藏 / 自定义名 / 赛道库映射都以**未压缩基名** `X.jsonl` 为键，归档只换后缀不丢标注。
