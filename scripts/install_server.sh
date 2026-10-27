#!/usr/bin/env bash
# 服务器安装：把仓库里的 board / collect 放到 /opt，并安装 systemd 与 cron。
# 前置：已填好仓库根目录的 config.env（从 config.env.example 复制）。
# 不碰 Caddy：反代片段在 board/caddy/xboard.caddy，按 docs/deployment.md 手动并入后 reload。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -f "$REPO_DIR/config.env" ]; then
  echo "先复制 config.env.example 为 config.env 并填好你的值，再跑本脚本。" >&2
  exit 1
fi

sudo mkdir -p /opt/xboard /opt/xcollect /var/www/x-publish-board/x /var/www/x-publish-board/xcollect
sudo cp "$REPO_DIR/board/render_board.py" "$REPO_DIR/board/state_server.py" /opt/xboard/
sudo cp "$REPO_DIR/collect/core.py" "$REPO_DIR/collect/ingest_server.py" "$REPO_DIR/collect/fx_poll.py" "$REPO_DIR/collect/package_extension.py" /opt/xcollect/
sudo cp "$REPO_DIR/config.env" /opt/xboard/config.env
sudo cp "$REPO_DIR/config.env" /opt/xcollect/config.env
sudo chmod 600 /opt/xboard/config.env /opt/xcollect/config.env

sudo cp "$REPO_DIR/board/systemd/xboard-state.service" "$REPO_DIR/board/systemd/xboard-render.service" "$REPO_DIR/board/systemd/xboard-render.timer" /etc/systemd/system/
sudo cp "$REPO_DIR/collect/systemd/xcollect.service" /etc/systemd/system/
sudo cp "$REPO_DIR/collect/cron/xcollect-fx.cron" /etc/cron.d/xcollect-fx

sudo systemctl daemon-reload
sudo systemctl enable --now xboard-state xcollect xboard-render.timer

echo "服务已起。接着：1) 按 docs/deployment.md 并入 Caddy 片段并 reload；"
echo "2) 手动渲染一次：sudo systemctl start xboard-render.service；"
echo "3) 按 docs/extension.md 生成并安装你的回采扩展。"
sudo systemctl --no-pager status xboard-state xcollect | head -n 30 || true
