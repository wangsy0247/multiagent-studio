#!/bin/bash
# ============================================
# MultiAgent Studio 一键启动脚本
# 启动顺序: Harness → App → Frontend
# ============================================

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "=========================================="
echo "  MultiAgent Studio 启动"
echo "=========================================="

# 0. 环境检查 (幂等 — 首次启动自动装依赖/生成配置, 之后秒过)
./setup.sh --non-interactive

# 1. Harness 服务 (Agent 运行时, 端口 8001)
echo ""
echo "[1/3] 启动 Harness 服务 (端口 8001)..."
# 必须在项目根目录运行，否则 Python 找不到 harness 包
conda run -n harness python -m harness.main &
HARNESS_PID=$!
sleep 3
echo "  Harness PID: $HARNESS_PID"

# 2. App 服务 (业务层, 端口 8000)
echo "[2/3] 启动 App 服务 (端口 8000)..."
conda run -n harness uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
APP_PID=$!
sleep 2
echo "  App PID: $APP_PID"

# 3. Frontend (Next.js 开发服务器, 端口 3000)
echo "[3/3] 启动前端 (端口 3000)..."
cd frontend
# 清理上次构建缓存，确保 Tailwind/CSS 变更生效
rm -rf .next
npm run dev &
FRONTEND_PID=$!
cd "$ROOT"
sleep 3

echo ""
echo "=========================================="
echo "  服务已启动！"
echo "=========================================="
echo "  前端:        http://localhost:3000"
echo "  App API:     http://localhost:8000/docs"
echo "  Harness API: http://localhost:8001/docs"
echo ""
echo "  PID: Harness=$HARNESS_PID App=$APP_PID Frontend=$FRONTEND_PID"
echo ""
echo "  停止所有服务: kill $HARNESS_PID $APP_PID $FRONTEND_PID"
echo "=========================================="

# 保存 PID 以便停止
echo "$HARNESS_PID $APP_PID $FRONTEND_PID" > /tmp/multiagent_studio.pids

# 等待任意进程退出
wait
