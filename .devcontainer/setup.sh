#!/bin/bash
# 第一次建立 Codespace 時自動執行：安裝 Claude Code、Codex、建模工具
set -x
bash "$(dirname "$0")/start.sh"   # 先讓工作台跑起來，其他東西在背景裝
sudo apt-get update
sudo apt-get install -y --no-install-recommends libgl1 libglu1-mesa libegl1 libxrender1 libxext6 libsm6 libxi6 libxft2 fonts-noto-cjk zip unzip || true
npm install -g @anthropic-ai/claude-code @openai/codex
touch /workspaces/.ms-data/.step_cli
pip install --no-cache-dir cadquery trimesh shapely manifold3d svgelements pillow matplotlib numpy scipy networkx rtree || true
touch /workspaces/.ms-data/.step_py
sudo apt-get install -y --no-install-recommends prusa-slicer || true
touch /workspaces/.ms-data/.step_done
