#!/usr/bin/env bash
# GT7 Dash 部署脚本 —— G8
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
# 现在的做法：
#   用 `git archive` 导出**指定 commit** 的快照（天然 LF、天然不含
#   .git / __pycache__ / 本地临时文件），打包传上去解到一个按版本命名的
#   目录，再用仓库里的 Dockerfile 构建。线上到底跑的哪个 commit，
#   看 `current` 软链 + `/api/health` 的 version 就知道。
#
# 用法：
#   tools/deploy.sh                     # 部署当前 HEAD
#   tools/deploy.sh --allow-dirty       # 工作区有改动也部署（不推荐）
#   tools/deploy.sh --base-image gt7-base:pre-w3 --skip-apt
#                                       # 无外网时：用一个已装好 tini 的基镜像
#   tools/deploy.sh --no-restart        # 只上传+构建，不重启容器
#
# 环境变量：
#   GT7_HOST      默认 192.168.43.18
#   GT7_USER      默认 root
#   GT7_PORT      默认 22
#   GT7_ROOT      服务器上的部署根目录，默认 /opt/gt7-dash
#   GT7_VERIFY_URL 部署后校验 /api/health 的地址，默认 http://127.0.0.1:8787
set -euo pipefail

HOST="${GT7_HOST:-192.168.43.18}"
USER_="${GT7_USER:-root}"
PORT="${GT7_PORT:-22}"
ROOT="${GT7_ROOT:-/opt/gt7-dash}"
VERIFY_URL="${GT7_VERIFY_URL:-http://127.0.0.1:8787}"

ALLOW_DIRTY=0
RESTART=1
BASE_IMAGE=""
SKIP_APT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --no-restart)  RESTART=0 ;;
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
REL="$ROOT/releases/v$VERSION-$COMMIT"
TARBALL="$(mktemp -t gt7-deploy-XXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT

echo "部署 v$VERSION ($COMMIT, $BRANCH) → $USER_@$HOST:$REL"

# ---------------------------------------------------------------- 打包快照
# 🔴 必须用 git archive，不能用 tar/scp 工作区：
#    只有 git archive 能保证「传上去的字节 == 索引里的字节」。
git archive --format=tar.gz -o "$TARBALL" HEAD
echo "  快照 $(du -h "$TARBALL" | cut -f1)，来自 commit $COMMIT"

SSH=(ssh -p "$PORT" -o StrictHostKeyChecking=accept-new "$USER_@$HOST")

# ---------------------------------------------------------------- 上传解包
"${SSH[@]}" "mkdir -p '$ROOT/releases' '$ROOT/data'"
scp -P "$PORT" -q "$TARBALL" "$USER_@$HOST:/tmp/gt7-release.tar.gz"
"${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
rm -rf '$REL'
mkdir -p '$REL'
tar -xzf /tmp/gt7-release.tar.gz -C '$REL'
rm -f /tmp/gt7-release.tar.gz

# 换行复核：CRLF 的 shell 脚本在 Linux 上会 `bad interpreter`。
# 这里当场查一遍，别等容器起不来再回头猜。
if grep -rlI \$'\r' '$REL' --include='*.sh' --include='*.py' 2>/dev/null | head -5 | grep -q .; then
    echo "✗ 解包后的文件里还有 CRLF，检查 .gitattributes 与 git archive 行为" >&2
    exit 1
fi
echo "  已解包到 $REL（换行检查通过）"
REMOTE

# ---------------------------------------------------------------- 构建镜像
BUILD_ARGS=()
[ -n "$BASE_IMAGE" ] && BUILD_ARGS+=(--build-arg "BASE_IMAGE=$BASE_IMAGE")
[ "$SKIP_APT" -eq 1 ] && BUILD_ARGS+=(--build-arg "SKIP_APT=1")

echo "  构建镜像 gt7-dash:v$VERSION"
"${SSH[@]}" "cd '$REL' && docker build ${BUILD_ARGS[*]:-} -t gt7-dash:v$VERSION -t gt7-dash:latest ."

# ---------------------------------------------------------------- 切换+重启
"${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
ln -sfn '$REL' '$ROOT/current'
cd '$ROOT/current'
# compose 里的 image 是 ghcr 上的名字，本地构建的是 gt7-dash:v$VERSION，
# 用环境变量覆盖，避免"改了 tag 但 compose 还拉旧镜像"。
export GT7_IMAGE='gt7-dash:v$VERSION'
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

echo "完成：$REL"
