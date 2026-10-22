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

# 🔴 基础镜像可替换（G8）：
#    服务器常在内网/无外网出口，`apt-get update` 会挂，于是过去只能在服务器上
#    手写一个「叠加式」Dockerfile（FROM 一个手工 tag 的基镜像 + COPY 几个 .py）。
#    那套东西不在仓库里 → 服务器跑的东西和仓库里的 Dockerfile 早就不是一回事，
#    而且没人能重建。
#
#    现在把差异做成**构建参数**，仍然只用这一份 Dockerfile：
#      --build-arg BASE_IMAGE=gt7-base:pre-w3   用一个已装好 tini 的基镜像
#      --build-arg SKIP_APT=1                   跳过 apt（基镜像里已经有了）
#    默认值保持"从官方 python 镜像全量构建"，联网环境下行为不变。
# 🔴 按 **digest** 固定，不用浮动 tag（G4）。
#    浮动 tag 意味着今天构建和下周构建拿到的是不同的基础镜像 ——
#    "同一个 commit 两次构建出不同产物"是排查线上问题时的噩梦。
#    这里钉的是 multi-arch index 的 digest，amd64 / arm64 都能用。
#
#    升级基础镜像时（有意为之的动作）：
#      1. 换掉下面的 sha256；
#      2. 跑一遍 docker build 确认 tini 仍能装上、-selftest 仍通过；
#      3. 在 CHANGELOG 里记一笔。
#    查当前 digest：
#      docker buildx imagetools inspect python:3.12-slim --format '{{.Manifest.Digest}}'
ARG BASE_IMAGE=python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

# ---------------------------------------------------------------- 阶段 1
# 编译 Go 解密器。
#
# 🔴 必须**在镜像里编译**，不能 COPY 仓库里的预编译二进制（以前就是那样，
#    一个手工编译的 1.6MB 文件躺在 gt7-decrypt/ 下）。后果：
#    改了 main.go（C 包的 0xDEADBEEF mask）→ CI 全绿 → 部署成功 →
#    C 包一个都解不开，因为镜像里还是上周那个二进制。
#    这种"改了代码但产物没变"的问题在 CI 上完全看不出来。
#
#    多阶段构建后：代码改动必然进镜像，仓库里也不再需要二进制产物。
ARG VERSION=dev
# 🔴 版本必须 >= gt7-decrypt/go.mod 里 `go` 指令要求的版本。
#    用低了会在 `go mod download` 阶段直接失败：
#    "go.mod requires go >= 1.26.0 (running go 1.22.12)"。
#    这条约束由 tests/test_deploy_contract.py 守着，改 go.mod 时不会忘。
FROM golang:1.26-alpine AS gobuild
WORKDIR /src
# 先只拷 go.mod/go.sum 再 download：改 main.go 时这一层能命中缓存
COPY gt7-decrypt/go.mod gt7-decrypt/go.sum ./
RUN go mod download
COPY gt7-decrypt/main.go ./
# -trimpath 去掉本机路径；-s -w 去符号表；-X main.version 烧版本号
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" \
    -o /gt7-decrypt .

# ---------------------------------------------------------------- 阶段 2
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="gt7-recorder" \
      org.opencontainers.image.description="GT7 遥测接收服务，常驻监听 UDP 广播并按场次落盘"

# 只用标准库，不需要额外依赖，所以镜像可以很精简。
# 唯一额外装的tini 是为了正确处理 SIGTERM —— Docker 停止容器时
# 靠它把信号转给 Python，才能触发 _end_session() 落盘文件尾。
ARG SKIP_APT=0
RUN if [ "$SKIP_APT" = "1" ]; then \
        echo "跳过 apt（基镜像已含 tini）"; \
    else \
        apt-get update \
        && apt-get install -y --no-install-recommends tini \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

# 先拷代码再设权限，利于利用构建缓存
COPY gt7-recorder.py gt7-dashboard.py gt7analysis.py gt7_version.py gt7-event-detector.py ./

# —— Salsa20 解密器 ——
# 🔴 这是必需的：GT7 真机发的包 100% 是 Salsa20 加密的。
#   自己手写Salsa20 极易出错（nonce 布局、轮函数顺序都可能错，
#   我试过两版都算不出正确密钥流），所以用 Go 的标准库编译成静态二进制。
#   交叉编译时必须显式设 GOOS/GOARCH，否则会出 PE 格式无法在 Linux 运行。
COPY --from=gobuild /gt7-decrypt /app/gt7-decrypt
# 🔴 自检失败必须让**构建失败**，不能 `|| echo "提示"` 吞掉。
#    原来那行 `|| echo "(自检提示：真实包解密已在部署时验证过)"` 等于：
#    "必需组件测了，但测不过也照样发布" —— 那这个检查就没有任何意义，
#    反而给人"已经验证过"的错觉。宁可构建红，也不要静默出一个解不开包的镜像。
RUN chmod 755 /app/gt7-decrypt && /app/gt7-decrypt -selftest

# 数据目录（用 volume 挂到宿主机，容器重建不丢数据）
# ⚠️ 这行只在**不用 bind mount** 时有效：./data:/data 会把它整个遮住，
#    容器看到的仍是宿主机目录的权限。README 要求部署前 chown 10001:10001。
RUN mkdir -p /data && chmod 777 /data

# 非 root 运行。用固定 UID/GID 便于宿主机上管理文件权限。
RUN useradd --uid 10001 --create-home gt7 \
    && chown -R gt7:gt7 /app /data
USER gt7

# 健康检查：查 status.json 的 **mtime**，用它回答"recorder 进程还活着吗"。
#
# 🔴 这条检查**不是**"PS5 在发数据吗"，两件事必须分开（混过一次，见下）：
#    · mtime 新鲜  → recorder 主循环还在跑（本检查）
#    · 真的在收包   → `/api/health` 的 `last_packet_age` 或 status.json 的 `t`
#
#    为此 recorder 主循环里加了 `_touch_status_alive()`：每 20 秒 touch
#    一次 mtime。**没有它这条检查根本不成立** —— `_write_status()` 只在
#    收到遥测包时被调用，PS5 一关机 mtime 就冻住，容器会在 PS5 停机
#    期间一直 unhealthy。而 HEALTHCHECK 要报的是"我的进程死了没有"，
#    不是"用户开没开游戏机"；后者天天红，久了就没人看了。
#    （这一条在计划文档里写反了，实测确认后才纠正过来。）
#
# 为什么用 mtime 而不是 `os.access('/data', W_OK)`：
#    原来只判目录可写 —— 那个属性**在容器整个生命周期内都不会变**，
#    等于一个永远健康的常量，测不出任何东西。mtime 至少能发现
#    "主循环卡死 / 进程被杀"。
#
# 120 秒阈值 vs 20 秒 touch 间隔：留 6 倍余量，避免磁盘抖动造成假红。
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,time; p='/data/status.json'; \
        sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<120 else 1)"

# 说明：真正的端口在 compose 里由 host 网络直接占用，这里仅作文档
EXPOSE 33739/udp
EXPOSE 33740/udp
# 仪表盘 HTTP 端口
EXPOSE 8787/tcp

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "gt7-recorder.py", "--output", "/data", "--verbose"]