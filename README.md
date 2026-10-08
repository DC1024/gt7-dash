# GT7 Dash — 跑车浪漫旅 7 实时遥测仪表盘

**PS5《Gran Turismo 7》官方 UDP 遥测 → 本地解密 → Web 实时仪表盘 + 公开数据 API。**
单文件服务、Docker 一键部署、全部数据本地处理、不依赖任何第三方服务。

![GitHub Actions](https://img.shields.io/github/actions/workflow/status/DC1024/gt7-dash/docker.yml?branch=main)
![GHCR](https://img.shields.io/badge/镜像-ghcr.io-blue)
![License](https://img.shields.io/badge/许可-MIT-green)

## ⬇️ 下载（Windows 免安装）

不想用 Docker 的话，可直接从 [Releases](https://github.com/DC1024/gt7-dash/releases)
下载 `gt7-dash-windows-x64.zip`：绿色免安装，含仪表盘 / 接收器 / 解密器三个 exe。
解压后先双击 `run-recorder.bat` 接收数据，再双击 `run-dashboard.bat` 打开
http://127.0.0.1:8787 查看。

## ✨ 特性

- **实时仪表盘**：转速表 / 速度 / 档位 / 踏板 / G 力球（随 G 值摇晃）/
  G-G 图（抓地力圆）/ 行车轨迹（按 G 力着色）/ 四轮温度 / 油量 / 涡轮 / 圈速列表
- **每圈圈速**：自动记录本场每一圈，最快圈高亮
- **场次自动切分**：以「是否在赛道上」为边界，跑完回菜单自动结束，每场比赛独立归档
- **场次管理**：历史场次的收藏 / 改名 / 删除（移入 `_trash` 可找回）；
  场次默认按「车型 + 采集时间 + 最快圈速」模板命名（列表页可自定义模板）；
  回收站支持恢复 / 彻底删除 / 一键清空，保留天数可调（0 = 永不自动清理）
- **可自定义布局**：卡片拖拽排序、隐藏/显示、半宽/整行，多套布局保存切换
- **个性化外观**：9 套主题预设（跟随系统 / 浅色 / 深色 / OLED 赛道黑 / 米黄护眼 /
  GT 竞速 / 冰川蓝 / HUD 荧光 / 午夜紫）、强调色自选、卡片间距与读数大小、
  速度单位 km/h⇄mph、实时曲线窗口与线条开关、G 力量程，偏好存本浏览器并可导出/导入
- **公开 API**：`/api/v1/*` 提供解密后的结构化数据（带 CORS），第三方程序直接消费
- **数据本地化**：Salsa20 在本地解密，无云端、无第三方依赖

## 🚀 快速开始

PS5 → 设置 → 网络 → 打开 GT7 遥测上传功能，然后：

```bash
mkdir gt7-dash && cd gt7-dash
curl -O https://raw.githubusercontent.com/DC1024/gt7-dash/main/docker-compose.yml
docker compose up -d
# 打开 http://<本机IP>:8787
```

> ⚠️ 两个服务都使用 `network_mode: host`——GT7 通过 UDP 广播发遥测，bridge 网络收不到。

## 🔌 数据 API

解密后的结构化数据通过 HTTP 公开（带 CORS），示例：

```bash
curl "http://<本机IP>:8787/api/v1/live?frames=120"   # 实时遥测
curl "http://192.168.x.x:8787/api/v1/laps"           # 每圈圈速
curl "http://192.168.x.x:8787/api/v1/sessions"       # 历史场次
```

完整字段与单位说明见 [`API文档.md`](API文档.md)，或直接访问 `/api/v1/docs`。

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

## 📁 项目结构

```
gt7-recorder.py        遥测接收 / 解密 / 场次判定 / 落盘
gt7-dashboard.py       Web 仪表盘 + 公开 API v1
gt7-decrypt/           Salsa20 解密器（Go，静态编译）
gt7-diagnose.py        收包诊断工具
gt7-event-detector.py  事件检测（打滑 / 急刹等）
gt7-overtake-detector.py  超车检测
API文档.md             公开 API 完整文档
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

## 🙏 致谢

- [InvoGT](https://github.com/InvolveDanny/InvoGT) — 字段偏移的权威参考
- [go-gt7-telemetry](https://github.com/snipem/go-gt7-telemetry) — 解密参数
- [Nenkai/gt7-udp](https://github.com/Nenkai) — 早期协议逆向

## 许可

[MIT](LICENSE)
