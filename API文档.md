# GT7 遥测公开 API v1

给第三方程序读取**已解密、已解析**的 GT7 遥测数据。
所有接口均为 GET，返回 UTF-8 JSON；全部带 CORS 头，浏览器端可直接调用。

- Base URL: `http://localhost:8787`（局域网内其他机器用 `http://<服务器IP>:8787`）
- 数据源：GT7 官方 UDP 遥测（33740 端口，Salsa20 本地解密），60 帧/秒
- 本文档也可通过 `GET /api/v1/docs` 获取（text/markdown）

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/live?frames=N` | 实时遥测：最新一帧 + 最近 N 帧历史 + 轨迹 + G-G 散点 |
| GET | `/api/v1/laps` | 当前场次的每圈成绩与最快圈 |
| GET | `/api/v1/sessions` | 历史场次文件列表 |
| GET | `/api/v1/sessions/<文件名>` | 指定场次的统计摘要 |
| GET | `/api/v1/sessions/<文件名>/download` | 下载原始 jsonl（attachment） |
| POST | `/api/v1/sessions/<文件名>/favorite` | 收藏/取消收藏，body `{"value": true}` |
| POST | `/api/v1/sessions/<文件名>/rename` | 改名，body `{"value": "新名称"}` |
| POST | `/api/v1/sessions/<文件名>/delete` | 删除（移入服务器 `data/_trash/`，保留期后自动清除） |
| GET | `/api/v1/settings` | 回收站保留期/清单与场次命名模板 |
| POST | `/api/v1/settings/trash-retention` | 设置回收站保留天数，body `{"value": 30}`（0=永不清理） |
| POST | `/api/v1/settings/name-template` | 场次默认命名模板，body `{"value": "{车型} {时间} {最快圈}"}` |
| POST | `/api/v1/trash/purge` | 清空回收站（彻底删除全部） |
| POST | `/api/v1/trash/<文件名>/restore` | 从回收站恢复场次到列表 |
| POST | `/api/v1/trash/<文件名>/delete` | 彻底删除回收站中的单个场次 |
| GET | `/api/v1/docs` | 本文档 |

`favorite` 与 `custom_name` 会合并在 `GET /api/v1/sessions` 的返回里
（`favorite: bool`、`custom_name: string`），收藏的场次排在最前。
列表每项还带 `car_name`（车型短名，从 `cars.csv` 查 ShortName，查不到为空串）。

### 参数

- `frames=N`：历史帧数，live 默认 120、上限 600；sessions 详情默认 0（不带回帧）
- `ref_lap=N`（sessions 详情 / 详情页）：指定参考圈号做行车轨迹/时间差对比分析，
  默认取最快圈；`cmp_lap=M`：指定被对比的圈，默认取最后一圈。
  圈号不存在或非有效（如手改 URL）时静默回退默认值。
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
| `car_code` | int | 车型码（全场非 0 众数；读不到为 0） |
| `car_name` | string | 车型短名，`cars.csv` 查 ShortName，查不到空串 |
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
| `gear` / `suggested_gear` | int | 当前档 / 建议档，0 = 空挡 |
| `throttle` / `brake` | 0~1 | 踏板开度（乘 100 即百分比） |
| `position_m` | 米 | 车辆世界坐标 x/y/z（y 为高度） |
| `velocity_ms` | m/s | 速度矢量（模长 ≈ speed_kph/3.6） |
| `g_force.longitudinal` | g | 纵向：正值加速、负值刹车 |
| `g_force.lateral` | g | 横向：**正值右转、负值左转** |
| `g_force.magnitude` | g | 合力大小 |
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
| `wheel_rev_per_s` | 转/秒 | 四轮转速（带符号，倒挡为负） |
| `flags` | bit 位 | bit0 在赛道 / bit1 暂停 / bit2 加载 / bit3 在挡 … |
| `state.on_track` | bool | 是否在赛道上（比赛进行中） |

### `timing`
| 字段 | 单位 | 说明 |
|---|---|---|
| `current_lap` | int | 当前圈号 |
| `laps_in_race` | int | 本局总圈数 |
| `best_lap_ms` / `last_lap_ms` | 毫秒 | 最快圈 / 上一圈（null = 还没跑完） |
| `current_lap_time_s` | 秒 | 本圈已用时 |

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

## 历史场次的逐帧数据

场次 jsonl 里每帧有四十几个字段（速度/转速/档位/油门/刹车/G 力/四轮/油量…）。
下面三个接口把它按不同粒度暴露出来，供详情页与第三方分析使用。

| 接口 | 说明 |
|---|---|
| `GET /api/v1/sessions/<文件名>/series?lap=N&max_points=2400` | 整场（或第 N 圈）的**降采样**时序，用于画曲线 |
| `GET /api/v1/sessions/<文件名>/frames?offset=0&limit=200&lap=N` | **分页**逐帧数据（`limit` 上限 1000），用于表格 |
| `GET /api/v1/sessions/<文件名>/csv?lap=N` | 全量 CSV 下载（带 UTF-8 BOM，Excel 直接打开不乱码） |
| `GET /api/v1/sessions/<文件名>/raceline?lap=N` | 第 N 圈的**行车轨迹**（踏板 + G 力两套着色通道）；`lap` 缺省 = 最快圈 |
| `GET /api/v1/sessions/<文件名>/sectors?n=4` | **分段计时 + 理论最快圈**；`n` = 段数（2~10，缺省 4） |
| `GET /api/v1/sessions/<文件名>/slip?max_points=120` | **轮胎滑移**：空转 / 抱死检测；每圈曲线最多 `max_points` 点 |

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
