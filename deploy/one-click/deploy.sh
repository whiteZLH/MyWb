#!/bin/sh
# MyWb 一键部署：自动生成密钥、构建镜像、启动管理后台。
# 用法：sh deploy/one-click/deploy.sh
#
# 本脚本只用 POSIX sh 语法，OpenWrt 的 busybox ash 可直接运行，无需安装 bash。
#
# 数据默认落系统目录（默认 /opt/mywb），与代码仓库分离，
# 避免 git clean / 删除仓库 / 重新 clone 时连数据与密钥一起丢失。
# 可用环境变量覆盖：
#   MYWB_HOME=/your/path sh deploy.sh        数据目录（默认 /opt/mywb）
# 也可用 -f 指定自定义 compose 文件（须作为第一个参数）：
#   sh deploy.sh -f docker-compose.custom.yml
set -eu

cd "$(dirname "$0")"
ROOT="$PWD"

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
用法：sh deploy.sh [-f <compose 文件>] [docker compose 透传参数]
  -f, --file  指定 compose 文件（默认 docker-compose.yml，须放在第一个参数）
  MYWB_HOME   数据目录，默认 /opt/mywb（可写 /etc/mywb/mywb.env 持久覆盖）
EOF
    exit 0 ;;
esac

# 数据目录：显式环境变量 > /etc/mywb/mywb.env > 默认 /opt/mywb
if [ -z "${MYWB_HOME:-}" ] && [ -f /etc/mywb/mywb.env ]; then
  # shellcheck disable=SC1091
  . /etc/mywb/mywb.env
fi
DATA_DIR="${MYWB_HOME:-/opt/mywb}"
MYWB_HOME="$DATA_DIR"
export MYWB_HOME
ENV_FILE="$DATA_DIR/.env"
COMPOSE_FILE="docker-compose.yml"

# -f/--file 只识别为第一个参数，其余参数原样透传给 docker compose up
if [ "${1:-}" = "-f" ] || [ "${1:-}" = "--file" ]; then
  [ -n "${2:-}" ] || { echo "错误：$1 需要一个参数。" >&2; exit 1; }
  COMPOSE_FILE="$2"
  shift 2
fi

# ---------- 前置检查 ----------
if ! command -v docker >/dev/null 2>&1; then
  echo "错误：未找到 docker，请先安装 Docker。" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "错误：未找到 docker compose（v2 插件）。" >&2
  echo "      Docker Compose v1 请改用 docker-compose，或升级到 v2。" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "错误：Docker 守护进程不可用（docker info 失败），请先启动 dockerd / Docker Desktop。" >&2
  exit 1
fi

# ---------- 目录初始化（失败多为权限不足） ----------
if ! mkdir -p "$DATA_DIR" 2>/dev/null; then
  echo "错误：无法创建数据目录 $DATA_DIR，请用 root 运行，或设置 MYWB_HOME 指向可写目录。" >&2
  echo "      示例：MYWB_HOME=\$HOME/mywb sh deploy.sh" >&2
  exit 1
fi

# 生成 URL 安全的随机串。显式判断命令是否存在：POSIX sh 无 pipefail，
# 用 `cmd | head` 的管道返回值会掩盖 cmd 失败，故不能靠 `||` 回落。
rand() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets; print(secrets.token_urlsafe($1))"
  elif command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -dc 'A-Za-z0-9_-' | head -c "$1"
  else
    head -c 96 /dev/urandom | tr -dc 'A-Za-z0-9_-' | head -c "$1"
  fi
}

GENERATED=0
if [ ! -f "$ENV_FILE" ]; then
  ADMIN_KEY="$(rand 36)"
  CLIENT_KEY="sk-wb-$(rand 32)"
  printf 'ADMIN_KEY=%s\nCODEBUDDY2OPENAI_KEY=%s\nPORT=8787\n' "$ADMIN_KEY" "$CLIENT_KEY" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  GENERATED=1
fi

# 数据子目录：Docker 自动创建的 bind 目录默认 0755，会让 auth/*.info（含 token）
# 对同机其它用户可读；server 侧 mkdir(mode=0o700) 对「已存在」目录不生效，
# 所以必须在这里先建好。非 root 且目录归属其它用户时降级为提示，不阻断部署。
mkdir -p "$DATA_DIR/auth" "$DATA_DIR/management"
chmod 700 "$DATA_DIR" "$DATA_DIR/auth" "$DATA_DIR/management" 2>/dev/null \
  || echo "提示：未能收紧 $DATA_DIR 权限（可能非 root），如为多用户主机请手动 chmod 700。" >&2

docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build "$@"

PORT="$(grep '^PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
PORT="${PORT:-8787}"

# 探测本机局域网地址，便于同网段设备直接访问（逐级回落，输出非空才采用）
lan_ip() {
  _out=""
  if command -v ip >/dev/null 2>&1; then
    _out="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
  fi
  if [ -z "$_out" ] && command -v hostname >/dev/null 2>&1; then
    _out="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | head -n1)"
  fi
  if [ -z "$_out" ] && command -v ifconfig >/dev/null 2>&1; then
    _out="$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127\.' | head -n1)"
  fi
  printf '%s' "$_out"
}
LANIP="$(lan_ip || true)"
LANIP="${LANIP:-<本机IP>}"

echo
echo "管理后台已启动："
echo "  本机       http://127.0.0.1:${PORT}/admin/"
echo "  局域网     http://${LANIP}:${PORT}/admin/"
if [ "$GENERATED" = "1" ]; then
  echo
  echo "首次部署已生成密钥（同时保存在 $ENV_FILE，仅创建时显示一次）："
  echo "  管理密钥   ADMIN_KEY            : $(grep '^ADMIN_KEY=' "$ENV_FILE" | cut -d= -f2)"
  echo "  客户端 Key CODEBUDDY2OPENAI_KEY : $(grep '^CODEBUDDY2OPENAI_KEY=' "$ENV_FILE" | cut -d= -f2)"
fi
echo
echo "持久化目录（与代码仓库分离，删仓库/重新 clone 不影响）："
echo "  $DATA_DIR/.env                配置（改端口/开关后重跑本脚本）"
echo "  $DATA_DIR/auth/               账号登录态"
echo "  $DATA_DIR/management/mywb.db  SQLite：账号索引、API Key、统计"
echo "  设置 MYWB_HOME 可改为其它位置；把 MYWB_HOME=该目录 写入 /etc/mywb/mywb.env 可持久生效。"
echo
echo "停止：cd $ROOT && docker compose -f $COMPOSE_FILE --env-file $ENV_FILE down"
echo "提示：管理后台为明文 HTTP，局域网内任意设备均可访问；跨不可信网络请在前端加 HTTPS"
echo "      反向代理（示例见 deploy/admin/nginx.conf.example），并设置 ADMIN_SECURE_COOKIE=1。"
