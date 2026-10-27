#!/usr/bin/env bash
# 无服务器模式一键启动（macOS / Linux）：在本机同时拉起状态服务、回采接收端
# 与本地网关，并先渲染一次发布板。Ctrl+C 全部停掉。
#
# 前置：仓库根目录有 config.env（本地版从 local/config.local.example 复制改），
#       lark-cli 已在本机以你的飞书账号登录，python3 可用。
# 启动后打开：http://127.0.0.1:8799/x/
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

CONFIG_FILE="${XBOARD_CONFIG:-$REPO_DIR/config.env}"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "找不到 $CONFIG_FILE" >&2
  echo "先复制一份本地配置：cp local/config.local.example config.env，然后填飞书与账号的值。" >&2
  exit 1
fi
set -a; . "$CONFIG_FILE"; set +a

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "需要 python3（3.10+）。" >&2; exit 1; }

mkdir -p "${XBOARD_OUT_DIR:-./data/board}" \
         "$(dirname "${XBOARD_TOKEN_FILE:-./data/board-state/state-token.txt}")" \
         "${XCOLLECT_STATE_DIR:-./data/collect}" \
         "$(dirname "${XCOLLECT_TOKEN_FILE:-./data/collect/collect-token.txt}")" \
         "$(dirname "${EXTENSION_ZIP_OUT:-./data/xcollect/extension.zip}")" \
         ./data/logs

echo "== 首次渲染发布板 =="
if ! "$PY" board/render_board.py; then
  echo "渲染失败（多半是 lark-cli 未登录或飞书配置没填对）。服务仍会启动，" >&2
  echo "修好后在仓库根目录手动跑一次：python3 board/render_board.py" >&2
fi

PIDS=()
cleanup() { kill "${PIDS[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

"$PY" board/state_server.py  >> ./data/logs/state.log  2>&1 & PIDS+=($!)
"$PY" collect/ingest_server.py >> ./data/logs/ingest.log 2>&1 & PIDS+=($!)
"$PY" local/gateway.py       >> ./data/logs/gateway.log 2>&1 & PIDS+=($!)
sleep 1

PORT="${XLOCAL_PORT:-8799}"
echo ""
echo "已启动（日志在 data/logs/）："
echo "  发布板（本机）： http://127.0.0.1:${PORT}/x/"
echo "  回采令牌文件：   ${XCOLLECT_TOKEN_FILE:-./data/collect/collect-token.txt}（填进扩展 popup）"
echo "  想要手机访问 / https 地址：另开一个终端跑  tailscale funnel --bg ${PORT}"
echo "  停：Ctrl+C"
wait
