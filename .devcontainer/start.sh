#!/bin/bash
# 每次 Codespace 開機時自動執行：把登入資料放到不會消失的地方，然後在背景啟動工作台
D=/workspaces/.ms-data
mkdir -p $D/MAKERSENSE $D/workbench $D/claude $D/codex
for x in claude codex; do
  if [ -d "$HOME/.$x" ] && [ ! -L "$HOME/.$x" ]; then cp -rn "$HOME/.$x/." "$D/$x/" 2>/dev/null; rm -rf "$HOME/.$x"; fi
  ln -sfn "$D/$x" "$HOME/.$x"
done
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 自動抓最新版工作台（Claude 修好的東西開機就會更新）
git -C "$ROOT" pull --ff-only -q 2>/dev/null || true
pkill -f "ms-workbench-loop" 2>/dev/null || true
pkill -f "$ROOT/workbench/server.mjs" 2>/dev/null || true
nohup setsid bash -c "exec -a ms-workbench-loop bash -c 'while true; do node \"$ROOT/workbench/server.mjs\" >> /tmp/workbench.log 2>&1; sleep 1; done'" >/dev/null 2>&1 &
exit 0
