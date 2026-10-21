#!/usr/bin/env bash
# GT7 Dash 部署脚本 —— G8 / T3
# ================================================================
# 解决的问题：
#   以前部署是「把 Windows 工作区 scp 到服务器 /opt」。后果有三个：
#     1. 服务器上那份代码**不在 git 里**，谁改过、改了什么、能不能重建，
#        全都没有答案；
#     2. Windows 的 CRLF 会一起过去（索引里是 LF，工作区是 CRLF），
#        带 CRLF 的 .sh 在 Linux 上直接 `bad interpreter`；
#     3. 镜像靠服务器上手写的「叠加式 Dockerfile」构建 —— 那份文件不在
#        仓库里，于是「仓库能跑」和「线上能跑」是两件不相干的事。
#
# 两种部署模式：
#   --image（默认） 从 ghcr 拉 CI 构建好的镜像。这是**推荐路径**：
#                   线上跑的就是 CI 里那一次构建的产物，可复现。
#   --local-build   在服务器上用仓库的 Dockerfile 现构建。用于
#                   ghcr 拉不到（内网/离线）或要临时验证未发布的改动。
#
# 🔴 部署根目录统一为 /opt/gt7-recorder —— 与 manage.sh 一致。
#    历史上 deploy.sh 用 /opt/gt7-dash、manage.sh 用 /opt/gt7-recorder，
#    两个运维入口指向不同容器（T3）。现在只有一个。
#
# 用法：
#   tools/deploy.sh                          # 从 ghcr 拉 v<version> 并重启
#   tools/deploy.sh --no-restart             # 只拉镜像，不重启
#   tools/deploy.sh --local-build            # 服务器上现构建（离线兜底）
#   tools/deploy.sh --image ghcr.io/...:tag  # 指定镜像
#   tools/deploy.sh --allow-dirty            # 工作区有改动也部署（不推荐）
#
# 环境变量：
#   GT7_HOST      默认 192.168.43.18
#   GT7_USER      默认 root
#   GT7_PORT      默认 22
#   GT7_ROOT      部署根目录，默认 /opt/gt7-recorder
#   GT7_VERIFY_URL 校验地址，默认 http://127.0.0.1:8787
set -euo pipefail

HOST="${GT7_HOST:-192.168.43.18}"
USER_="${GT7_USER:-root}"
PORT="${GT7_PORT:-22}"
ROOT="${GT7_ROOT:-/opt/gt7-recorder}"
VERIFY_URL="${GT7_VERIFY_URL:-http://127.0.0.1:8787}"
REGISTRY_IMAGE="ghcr.io/dc1024/gt7-dash"

ALLOW_DIRTY=0
RESTART=1
MODE="image"          # image | local-build
IMAGE=""
BASE_IMAGE=""
SKIP_APT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --no-restart)  RESTART=0 ;;
        --local-build) MODE="local-build" ;;
        --image)       IMAGE="${2:?--image 需要一个值}"; shift ;;
        --base-image)  BASE_IMAGE="${2:?--base-image 需要一个值}"; shift ;;
        --skip-apt)    SKIP_APT=1 ;;
        -h|--help)     sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
    esac
    shift
done

cd "$(dirname "$0")/.."

# ---------------------------------------------------------------- 前置检查
if [ ! -d .git ]; then
    echo "✗ 当前目录不是 git 仓库 —— 本脚本的前提就是「部署 git 里的东西」" >&2
    exit 1
fi

if [ "$ALLOW_DIRTY" -eq 0 ] && [ -n "$(git status --porcelain)" ]; then
    echo "✗ 工作区有未提交的改动，拒绝部署。" >&2
    echo "  未提交的东西传上去，线上就跑着一个**不存在于任何 commit** 的版本。" >&2
    echo "  先提交，或加 --allow-dirty 明确表示你知道后果。" >&2
    git status --short >&2
    exit 1
fi

VERSION="$(python -c 'import gt7_version; print(gt7_version.__version__)')"
COMMIT="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [ "$MODE" = "image" ]; then
    IMAGE="${IMAGE:-$REGISTRY_IMAGE:v$VERSION}"
    echo "部署 $IMAGE → $USER_@$HOST:$ROOT"
else
    IMAGE="gt7-dash:v$VERSION"
    REL="$ROOT/releases/v$VERSION-$COMMIT"
    TARBALL="$(mktemp -t gt7-deploy-XXXXXX.tar.gz)"
    trap 'rm -f "$TARBALL"' EXIT
    echo "本地构建部署 v$VERSION ($COMMIT, $BRANCH) → $USER_@$HOST:$REL"
fi

SSH=(ssh -p "$PORT" -o StrictHostKeyChecking=accept-new "$USER_@$HOST")

# ------------------------------------------------------- 同步 compose 定义
# compose 是「跑什么」的唯一描述，必须来自仓库。
# 🔴 只传 compose，不传代码：镜像里已经有代码了，传两遍必然有一天不一致。
"${SSH[@]}" "mkdir -p '$ROOT' '$ROOT/data'"
scp -P "$PORT" -q docker-compose.yml "$USER_@$HOST:$ROOT/docker-compose.yml"
echo "  已同步 docker-compose.yml"

if [ "$MODE" = "local-build" ]; then
    # ------------------------------------------------------------ 本地构建
    # 🔴 必须用 git archive，不能用 tar/scp 工作区：
    #    只有 git archive 能保证「传上去的字节 == 索引里的字节」。
    git archive --format=tar.gz -o "$TARBALL" HEAD
    echo "  快照 $(du -h "$TARBALL" | cut -f1)，来自 commit $COMMIT"

    scp -P "$PORT" -q "$TARBALL" "$USER_@$HOST:/tmp/gt7-release.tar.gz"
    "${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
rm -rf '$REL'
mkdir -p '$REL'
tar -xzf /tmp/gt7-release.tar.gz -C '$REL'
rm -f /tmp/gt7-release.tar.gz
# 换行复核：CRLF 的 shell 脚本在 Linux 上会 `bad interpreter`。
if grep -rlI \$'\r' '$REL' --include='*.sh' --include='*.py' 2>/dev/null | head -5 | grep -q .; then
    echo "✗ 解包后的文件里还有 CRLF，检查 .gitattributes 与 git archive 行为" >&2
    exit 1
fi
echo "  已解包到 $REL（换行检查通过）"
REMOTE

    BUILD_ARGS=()
    [ -n "$BASE_IMAGE" ] && BUILD_ARGS+=(--build-arg "BASE_IMAGE=$BASE_IMAGE")
    [ "$SKIP_APT" -eq 1 ] && BUILD_ARGS+=(--build-arg "SKIP_APT=1")

    echo "  构建镜像 $IMAGE"
    "${SSH[@]}" "cd '$REL' && docker build ${BUILD_ARGS[*]:-} -t '$IMAGE' ."
else
    # ------------------------------------------------------------ 拉镜像
    # 🔴 不 pull 就直接 up 的话，compose 会静默沿用本地旧镜像，
    #    看起来"部署成功"、实际跑的还是上一版（踩过）。
    echo "  拉取镜像"
    "${SSH[@]}" "docker pull '$IMAGE'" 2>&1 | tail -3
fi

# ---------------------------------------------------------------- 切换+重启
"${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
cd '$ROOT'
export GT7_IMAGE='$IMAGE'
if [ '$RESTART' -eq 1 ]; then
    # 🔴 --no-deps：不带它的话，动 dashboard 会连带重建 recorder，
    #    正在跑的比赛会被切成两个文件（真实踩过，丢了 30 秒）。
    docker compose up -d --no-deps --force-recreate gt7-recorder gt7-dashboard
else
    echo "  --no-restart：跳过重启"
fi
REMOTE

# ---------------------------------------------------------------- 校验
if [ "$RESTART" -eq 1 ]; then
    echo "  校验线上版本"
    OUT="$("${SSH[@]}" "python3 - <<'PY'
import json, urllib.request
try:
    with urllib.request.urlopen('$VERIFY_URL/api/health', timeout=10) as r:
        print(json.load(r).get('version', ''))
except Exception as e:
    print('ERR', e)
PY")"
    if [ "$OUT" != "$VERSION" ]; then
        echo "✗ 线上版本是 '$OUT'，期望 '$VERSION' —— 部署没生效" >&2
        exit 1
    fi
    echo "✓ 线上 /api/health 报 v$OUT，与仓库一致"
fi

echo "完成：$IMAGE → $ROOT"
