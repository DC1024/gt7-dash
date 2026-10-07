#!/bin/bash
# GT7 遥测服务运维脚本
# ====================
# 部署在 Ubuntu 服务器 192.168.43.18:/opt/gt7-recorder
# 用法：./manage.sh {start|stop|restart|status|logs|health|test|dashboard|clean}

set -uo pipefail
DIR="/opt/gt7-recorder"
DATA="$DIR/data"
CONTAINER="gt7-recorder"
DASH="gt7-dashboard"
DASH_PORT=8787

cd "$DIR" || { echo "✗ 找不到 $DIR"; exit 1; }

case "${1:-status}" in
  start)
    echo "▶ 启动服务…"
    docker compose up -d
    sleep 3
    "$0" status
    ;;

  stop)
    echo "■停止服务…"
    docker compose down
    ;;

  restart)
    echo "↻ 重启服务…"
    docker compose restart
    sleep 3
    "$0" status
    ;;

  status)
    echo "═══ 服务状态 ═══"
    docker ps --filter "name=$CONTAINER" --filter "name=$DASH" \
      --format "{{.Names}} | {{.Status}}"
    echo ""
    echo "─── 端口监听 ───"
    if ss -ulnp 2>/dev/null | grep -qE '33739|33740'; then
      ss -ulnp 2>/dev/null | grep -E '33739|33740' | sed 's/^/  /'
    else
      echo "  ✗ UDP 33739/33740 未绑定！容器可能没起来"
    fi
    if ss -tlnp 2>/dev/null | grep -q ":$DASH_PORT"; then
      echo "  仪表盘 HTTP :$DASH_PORT 已监听"
    else
      echo "  ✗ 仪表盘端口 $DASH_PORT 未监听"
    fi
    ;;

  dashboard)
    echo "═══ 仪表盘 ═══"
    echo "  访问地址: http://192.168.43.18:$DASH_PORT"
    echo ""
    # 从本机探活，确认 API 能返回
    if curl -sf --max-time 5 "http://127.0.0.1:$DASH_PORT/api/health" >/dev/null 2>&1; then
      echo "  ✓ 服务响应正常"
      echo ""
      echo "  当前状态:"
      curl -s --max-time 5 "http://127.0.0.1:$DASH_PORT/api/health" | sed 's/^/    /'
      echo ""
      echo "  在浏览器打开上面的地址即可看到实时仪表盘。"
    else
      echo "  ✗ 无法访问，检查 docker logs $DASH"
    fi
    ;;

  logs)
    docker logs -f --tail 100 "$CONTAINER"
    ;;

  health)
    echo "═══ 健康检查 ═══"
    # 1. 容器在跑吗
    if docker ps --format '{{.Names}}' | grep -q "^$CONTAINER$"; then
      echo "  ✓ 容器运行中"
    else
      echo "  ✗ 容器未运行"
      return 1
    fi

    # 2. 端口绑了吗
    if ss -ulnp | grep -qE '33739|33740'; then
      echo "  ✓ UDP 端口已绑定"
    else
      echo "  ✗ UDP 端口未绑定"
    fi

    # 3. 网络配置三要素（GT7 三大坑）
    echo ""
    echo "  ── 需要你在PS5 侧确认 ──"
    echo "  1. PC/服务器 与 PS5 同一网段（192.168.43.x）"
    echo "  2. 不是访客 WiFi / VLAN / 交换机隔离"
    echo "  3. PS5 → 设置 → 网络 → 查看连接状态，记下 IP"

    # 4. 最近有没有数据
    echo ""
    echo "  ── 最近采集 ──"
    RECENT=$(find "$DATA" -name '*.jsonl' -mmin -30 2>/dev/null | grep -v status | head -1)
    if [ -n "$RECENT" ]; then
      LINES=$(wc -l < "$RECENT")
      if [ "$LINES" -gt 10 ]; then
        echo "  ✓ 30 分钟内有数据：$LINES 帧"
        echo "    文件: $(basename "$RECENT")"
        echo ""
        echo "  仪表盘: http://192.168.43.18:$DASH_PORT"
      else
        echo "  ⚠ 有文件但只有 $LINES 帧，PS5 可能没进赛道"
      fi
    else
      echo "  · 近 30 分钟无数据（PS5 未开或未进赛道）"
    fi
    ;;

  test)
    #注入测试包，验证收包链路是否正常（不依赖 PS5）
    echo "═══ 注入测试（验证收包链路）═══"
    python3 - <<'EOF'
import socket, struct, time, sys
def build(seq, lap=3):
    p = bytearray(420)
    p[0:4]=b"GT7\x00"; p[4]=1; p[5]=0
    p[6:8]=struct.pack("<H",0x0F); p[8:12]=struct.pack("<I",seq)
    p[12:16]=struct.pack("<I",999)
    struct.pack_into("<f",p,28,time.time()); struct.pack_into("<H",p,32,lap)
    struct.pack_into("<H",p,56,55000); struct.pack_into("<H",p,68,9000)
    struct.pack_into("<b",p,116,6); p[128]=235; p[129]=20
    struct.pack_into("<f",p,296,123.45); struct.pack_into("<f",p,300,-67.89)
    for i,g in enumerate((12,-45,5)):
        struct.pack_into("<b",p,304+i,max(-128,min(127,int(g/0.02))))
    for i,w in enumerate((210.0,208.0,205.0,203.0)):
        struct.pack_into("<H",p,272+i*4,int(w))
    return bytes(p)

# 单播到本机 IP（虚拟机不回环自己的广播，必须单播）
target = ("192.168.43.18", 33739)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for i in range(120):
    s.sendto(build(i), target)
    time.sleep(0.01)
print(f"  已注入 120 帧到 {target[0]}:{target[1]}")
EOF
    sleep 3
    echo ""
    F=$(ls -t "$DATA"/*.jsonl 2>/dev/null | head -1)
    if [ -n "$F" ]; then
      echo "  ✓ 最新文件: $(basename "$F")"
      echo "    行数: $(wc -l < "$F")"
    else
      echo "  ✗ 没有落盘文件，检查 docker logs"
    fi

    # 同时验证仪表盘能否读到——这是最容易坏的一环
    echo ""
    echo "  ── 仪表盘链路 ──"
    if [ -f "$DATA/status.json" ]; then
      SZ=$(stat -c%s "$DATA/status.json" 2>/dev/null || echo 0)
      echo "  ✓ 状态文件存在（$SZ 字节）"
      if [ "$SZ" -gt 500 ]; then
        H=$(curl -s --max-time 5 "http://127.0.0.1:$DASH_PORT/api/state?frames=300" 2>/dev/null)
        N=$(echo "$H" | grep -o '"speed_kph"' | wc -l)
        echo "  ✓ 仪表盘 API 返回 $N 帧历史"
      else
        echo "  ⚠ 状态文件太小，可能刚启动"
      fi
    else
      echo "  ✗ 状态文件不存在——接收器需要 --status-file 参数"
    fi
    ;;

  clean)
    echo "⚠ 即将删除所有已采集数据"
    du -sh "$DATA" 2>/dev/null
    read -p "  确认删除？(yes/no) " ans
    if [ "$ans" = "yes" ]; then
      rm -f "$DATA"/*.jsonl
      echo "  ✓ 已清理"
    else
      echo "  已取消"
    fi
    ;;

  *)
    echo "用法: $0 {start|stop|restart|status|logs|health|test|dashboard|clean}"
    echo ""
    echo "  dashboard  打开 Web 仪表盘地址并探活"
    echo "  test       注入测试包，验证收包 + 仪表盘两条链路"
    echo "  health     全面健康检查（含 PS5 网络三要素提示）"
    ;;
esac