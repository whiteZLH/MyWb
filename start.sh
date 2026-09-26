#!/usr/bin/env bash
# start.sh —— MyWb 一键启动（Linux / macOS）
#
# 首次运行会在脚本所在目录自动生成 .env（随机管理密钥与客户端 Key），随后启动服务。
# 所有数据（SQLite、登录态）都落在当前脚本目录；目录锚点即“当前目录”。
#
# 配置方式（可选）：
#   - 想自定义端口 / 开关，直接编辑生成的 .env（参考 .env.example）。
#   - 已存在 .env 时脚本不会覆盖，只负责启动。
#
# 用法：在项目根目录执行  bash start.sh

set -euo pipefail

# 以脚本所在目录作为运行目录（.env 与数据的锚点 = 当前目录）
cd "$(dirname "$0")"
ROOT="$PWD"

# 生成 URL 安全的随机串（优先 Python，回落到 openssl）
rand() {
  python3 -c "import secrets; print(secrets.token_urlsafe($1))" 2>/dev/null \
    || openssl rand -base64 48 | tr -dc 'A-Za-z0-9_-' | head -c "$1"
}

ENV_FILE="$ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  ADMIN_KEY="$(rand 36)"
  CLIENT_KEY="sk-wb-$(rand 32)"
  {
    echo "ADMIN_KEY=$ADMIN_KEY"
    echo "CODEBUDDY2OPENAI_KEY=$CLIENT_KEY"
    echo "PORT=8787"
    echo "ADMIN_SECURE_COOKIE=0"
    echo "CODEBUDDY_AUTH_DIR=$ROOT"
    echo "MANAGEMENT_DATA_DIR=$ROOT"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  echo "已生成 .env：$ENV_FILE"
  echo "  管理密钥   ADMIN_KEY             : $ADMIN_KEY"
  echo "  客户端 Key CODEBUDDY2OPENAI_KEY : $CLIENT_KEY"
  echo "  （请妥善保管，删除 .env 后会重新生成）"
  echo
fi

# 存在虚拟环境则激活；否则使用系统 Python（需已安装依赖）
if [ -f "$ROOT/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

echo "启动管理后台：http://127.0.0.1:8787/admin/ （Ctrl+C 停止）"
exec python3 -m admin.server
