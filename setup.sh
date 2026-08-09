#!/bin/bash
# ============================================
# MultiAgent Studio 首次安装/环境检查脚本 (幂等)
#   ./setup.sh                  交互式 (缺模型 API Key 时会询问)
#   ./setup.sh --non-interactive  非交互 (start.sh 调用; 缺 Key 只警告)
#
# 做的事:
#   1. 根 .env            — 不存在则从 .env.example 生成, 并生成随机 JWT_SECRET
#   2. harness/.env       — 不存在则从模板生成 (模型 API 由服务器统一配置)
#   3. harness/config.yaml — 不存在则从 config.example.yaml 生成 (沙箱/记忆等基础设施)
#   4. Python 依赖        — 缺失才 pip install
#   5. 前端依赖           — node_modules 不存在才 npm install
# ============================================

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

NON_INTERACTIVE=0
[ "${1:-}" = "--non-interactive" ] && NON_INTERACTIVE=1

info() { echo "  [OK] $1"; }
warn() { echo "  [警告] $1"; }

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 -c "import secrets; print(secrets.token_hex(32))"
  fi
}

# ── 0. 前置依赖: conda / node ──
if ! command -v conda >/dev/null 2>&1; then
  echo "错误: 未找到 conda。请先安装 Miniconda:"
  echo "  wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
  echo "  bash Miniconda3-latest-Linux-x86_64.sh -b -p ~/miniconda3"
  echo "  ~/miniconda3/bin/conda init bash && source ~/.bashrc"
  exit 1
fi
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "错误: 未找到 node/npm。请安装 Node.js >= 18.18 (推荐 LTS):"
  echo "  https://nodejs.org/ 或使用 nvm: nvm install --lts"
  exit 1
fi

# conda 环境不存在则自动创建 (全新服务器首次运行)
if ! conda env list | grep -qE "^harness\s"; then
  echo "首次运行: 创建 conda 环境 'harness' (python=3.12)..."
  conda create -n harness python=3.12 -y
fi

PY="conda run -n harness python"
PIP="conda run -n harness pip"

echo "=========================================="
echo "  MultiAgent Studio 环境检查"
echo "=========================================="

# ── 1. 根 .env (app 服务) ──
echo ""
echo "[1/5] 检查根 .env (app 服务配置)..."
if [ ! -f .env ]; then
  cp .env.example .env
  # 生成强随机 JWT_SECRET, 替换弱占位
  SECRET="$(rand_hex)"
  sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${SECRET}|" .env
  # 生成内部接口共享密钥
  INTERNAL_TOKEN="$(rand_hex)"
  sed -i "s|^INTERNAL_API_TOKEN=.*|INTERNAL_API_TOKEN=${INTERNAL_TOKEN}|" .env
  info "已生成 .env (含随机 JWT_SECRET / INTERNAL_API_TOKEN)"
else
  if grep -q "^JWT_SECRET=change-me-in-production" .env; then
    warn ".env 中的 JWT_SECRET 仍是弱占位 — 建议手动替换为随机串 (修改后旧登录态会失效)"
  fi
  info ".env 已存在, 跳过"
fi

# ── 2. harness/.env (模型 API — 服务器统一配置) ──
echo ""
echo "[2/5] 检查 harness/.env (模型 API 配置)..."
if [ ! -f harness/.env ]; then
  cp harness/.env.example harness/.env
  # 同步 INTERNAL_API_TOKEN (app 与 harness 需一致)
  ROOT_TOKEN="$(grep -E '^INTERNAL_API_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true)"
  if [ -n "$ROOT_TOKEN" ]; then
    echo "INTERNAL_API_TOKEN=${ROOT_TOKEN}" >> harness/.env
  fi
  if [ "$NON_INTERACTIVE" -eq 0 ] && [ -t 0 ]; then
    echo ""
    echo "  模型 API 由服务器统一配置 (所有用户共用)。"
    read -r -p "  请输入 OPENAI_API_KEY (留空稍后手动编辑 harness/.env): " API_KEY
    read -r -p "  请输入 OPENAI_BASE_URL [https://api.openai.com/v1]: " BASE_URL
    read -r -p "  请输入 DEFAULT_MODEL [gpt-4o]: " MODEL
    [ -n "$API_KEY" ] && sed -i "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=${API_KEY}|" harness/.env
    [ -n "$BASE_URL" ] && sed -i "s|^OPENAI_BASE_URL=.*|OPENAI_BASE_URL=${BASE_URL}|" harness/.env
    [ -n "$MODEL" ] && sed -i "s|^DEFAULT_MODEL=.*|DEFAULT_MODEL=${MODEL}|" harness/.env
  fi
  info "已生成 harness/.env"
else
  info "harness/.env 已存在, 跳过"
fi

if grep -q "^OPENAI_API_KEY=sk-your-api-key-here" harness/.env 2>/dev/null || ! grep -q "^OPENAI_API_KEY=.\+" harness/.env 2>/dev/null; then
  warn "harness/.env 的 OPENAI_API_KEY 未配置 — LLM 调用将失败, 请编辑后重启"
fi

# ── 3. harness/config.yaml (服务器基础设施配置 — 首次启动生成) ──
echo ""
echo "[3/5] 检查 harness/config.yaml (沙箱 / 记忆 / 存储等基础设施)..."
if [ ! -f harness/config.yaml ]; then
  cp harness/config.example.yaml harness/config.yaml
  if [ "$NON_INTERACTIVE" -eq 0 ] && [ -t 0 ]; then
    echo ""
    echo "  沙箱用于隔离执行 agent 的代码/文件操作, 请选择:"
    echo "    1) LocalSandbox 本地直接执行 (默认, 零依赖)"
    echo "    2) OpenSandbox  Docker 隔离 (需 Docker / 沙箱服务)"
    read -r -p "  沙箱类型 [1]: " SANDBOX_CHOICE
    if [ "${SANDBOX_CHOICE:-1}" = "2" ]; then
      sed -i 's|^  use: harness\.services\..*:.*Provider|  use: harness.services.open_sandbox_provider:OpenSandboxProvider|' harness/config.yaml
      info "沙箱: OpenSandbox (Docker 隔离; 服务不可达时自动回退本地)"
    else
      info "沙箱: LocalSandbox (本地执行)"
    fi
    read -r -p "  是否启用记忆功能 (file 后端) [Y/n]: " MEM_CHOICE
    if [ "${MEM_CHOICE:-Y}" = "n" ] || [ "${MEM_CHOICE:-Y}" = "N" ]; then
      sed -i '/^memory:/,/^# --- Summarization ---/ s|^  enabled: True|  enabled: False|' harness/config.yaml
      info "记忆: 已禁用"
    else
      info "记忆: 启用 (file 后端)"
    fi
  fi
  info "已生成 harness/config.yaml (服务器本地配置, 不纳入 git; 模板为 config.example.yaml)"
else
  info "harness/config.yaml 已存在, 跳过 (如需重配请编辑该文件)"
fi

# ── 4. Python 依赖 ──
echo ""
echo "[4/5] 检查 Python 依赖 (conda env: harness)..."
if $PY -c "import fastapi, uvicorn, langchain_openai, langgraph" >/dev/null 2>&1; then
  info "Python 依赖已就绪, 跳过安装"
else
  echo "  安装 Python 依赖 (harness/requirements.txt + app/requirements.txt)..."
  $PIP install -r harness/requirements.txt -r app/requirements.txt
  info "Python 依赖安装完成"
fi

# ── 5. 前端依赖 ──
echo ""
echo "[5/5] 检查前端依赖..."
if [ -d frontend/node_modules ]; then
  info "node_modules 已存在, 跳过安装"
else
  echo "  安装前端依赖 (npm install)..."
  (cd frontend && npm install)
  info "前端依赖安装完成"
fi

echo ""
echo "=========================================="
echo "  环境检查完成"
echo "=========================================="
