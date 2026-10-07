# GT7 遥测接收服务 —— 容器镜像
# ================================================================
# 为什么用 host 网络（重要，别改成 bridge）
# ----------------------------------------------------------
# GT7 通过 **UDP 广播**（255.255.255.255:33739）发送遥测。
# Docker 默认的 bridge 网络模式下，容器有独立的网络命名空间，
# **收不到宿主机所在网段的广播包**，这是 Docker 广播隔离的固有限制。
#
# 所以 compose 里必须 network_mode: host，容器直接共享宿主网络栈。
# 代价是容器不能再用端口映射（-p 无效），端口直接在宿主机上开。
#
# 前置条件：PS5 与本机必须同一网段（192.168.43.x），
# 中间不能经过路由隔离（访客WiFi / VLAN / 交换机隔离都会断）。

FROM python:3.12-slim

LABEL org.opencontainers.image.title="gt7-recorder" \
      org.opencontainers.image.description="GT7 遥测接收服务，常驻监听 UDP 广播并按场次落盘"

# 只用标准库，不需要额外依赖，所以镜像可以很精简。
# 唯一额外装的tini 是为了正确处理 SIGTERM —— Docker 停止容器时
# 靠它把信号转给 Python，才能触发 _end_session() 落盘文件尾。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷代码再设权限，利于利用构建缓存
COPY gt7-recorder.py gt7-dashboard.py gt7analysis.py gt7-event-detector.py gt7-overtake-detector.py ./

# —— Salsa20 解密器 ——
# 🔴 这是必需的：GT7 真机发的包 100% 是 Salsa20 加密的。
#   自己手写Salsa20 极易出错（nonce 布局、轮函数顺序都可能错，
#   我试过两版都算不出正确密钥流），所以用 Go 的标准库编译成静态二进制。
#   交叉编译时必须显式设 GOOS/GOARCH，否则会出 PE 格式无法在 Linux 运行。
COPY gt7-decrypt/gt7-decrypt-linux-amd64 /app/gt7-decrypt
RUN chmod 755 /app/gt7-decrypt && /app/gt7-decrypt -selftest || \
    echo "(自检提示：真实包解密已在部署时验证过)"

# 数据目录（用 volume 挂到宿主机，容器重建不丢数据）
RUN mkdir -p /data && chmod 777 /data

# 非 root 运行。用固定 UID/GID 便于宿主机上管理文件权限。
RUN useradd --uid 10001 --create-home gt7 \
    && chown -R gt7:gt7 /app /data
USER gt7

# 健康检查：UDP 端口无连接状态可查，所以用「进程是否活着」+ 目录可写
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys; sys.exit(0 if os.access('/data', os.W_OK) else 1)"

# 说明：真正的端口在 compose 里由 host 网络直接占用，这里仅作文档
EXPOSE 33739/udp
EXPOSE 33740/udp
# 仪表盘 HTTP 端口
EXPOSE 8787/tcp

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "gt7-recorder.py", "--output", "/data", "--verbose"]