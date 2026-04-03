#!/usr/bin/env bash
#
# Mark-L Agent — 环境初始化脚本
#
# 用法：  ./scripts/setup.sh
# 前置：  Python 3.10+, Node.js 20+（MCP 需要）
# 幂等：  可重复执行，已存在的文件/目录不会覆盖。
#
# 完成后需手动编辑：
#   - .env       — 填入飞书凭证和 API Key
#   - mcp.json   — 填入 MCP 凭证

set -euo pipefail
cd "$(dirname "$0")/.."

# ── 前置检查 ──

echo "=== Mark-L Agent Setup ==="
echo ""

# Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found, install Python 3.10+ first" >&2
    exit 1
fi

py_version=$(python3 -c 'import sys; print(sys.version_info >= (3, 10))')
if [ "$py_version" != "True" ]; then
    echo "ERROR: Python 3.10+ required, got $(python3 --version)" >&2
    exit 1
fi

# Node.js（非阻断，MCP 可选）
has_node=false
if command -v npx &>/dev/null; then
    has_node=true
fi

# ── 1. Python 虚拟环境 ──

if [ ! -d ".venv" ]; then
    echo "[1/5] Creating virtual environment..."
    python3 -m venv .venv
else
    echo "[1/5] Virtual environment exists, skipping"
fi

source .venv/bin/activate

# ── 2. 安装依赖 ──

echo "[2/5] Installing dependencies..."
pip install -q --upgrade pip
if [ -f "requirements.lock" ]; then
    pip install -q -r requirements.lock
else
    pip install -q -r requirements.txt
fi

# ── 3. 预下载 lark-mcp ──

if [ "$has_node" = true ]; then
    echo "[3/5] Pre-downloading lark-mcp..."
    npx -y @larksuiteoapi/lark-mcp --help >/dev/null 2>&1 || true
else
    echo "[3/5] SKIP: npx not found — install Node.js 20+ for MCP support"
fi

# ── 4. 运行时目录 ──

echo "[4/5] Creating runtime directories..."
mkdir -p data biz

# ── 5. 配置文件 ──

echo "[5/5] Preparing config files..."

copy_template() {
    local target="$1" template="$2"
    if [ -f "$target" ]; then
        echo "  $target exists, skipping"
    elif [ -f "$template" ]; then
        cp "$template" "$target"
        chmod 600 "$target"
        echo "  $target created from template ← please edit"
    else
        echo "  WARNING: $target missing, no template found"
    fi
}

copy_template .env .env.example
copy_template mcp.json mcp.json.example

# ── Done ──

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env      — fill FEISHU_APP_ID, FEISHU_APP_SECRET, ANTHROPIC_API_KEY"
echo "  2. Edit mcp.json  — fill APP_ID, APP_SECRET, MCP token"
echo "  3. source .venv/bin/activate && python main.py"
