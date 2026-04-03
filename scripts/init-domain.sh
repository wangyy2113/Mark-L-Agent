#!/usr/bin/env bash
#
# 初始化 biz 域的目录骨架 + 模板文件
#
# 用法：  ./scripts/init-domain.sh <domain-name>
# 环境：  BIZ_BASE_PATH=<path>  覆盖默认 biz/ 目录（同 core/config.py）
# 幂等：  已存在的文件不会覆盖。
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
source "$SCRIPT_DIR/env.sh"

# ── 参数检查 ──

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <domain-name>" >&2
    echo "Example: $0 ca" >&2
    exit 1
fi

DOMAIN="$1"
BIZ_DIR="$BIZ_BASE/$DOMAIN"

# 域名格式校验：仅允许小写字母、数字、连字符
if [[ ! "$DOMAIN" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "Error: domain name must be lowercase alphanumeric (with hyphens), got: $DOMAIN" >&2
    exit 1
fi

echo "=== Init Domain: $DOMAIN ==="
echo ""

# ── _base 共享上下文 ──

BASE_DIR="$BIZ_BASE/_base"
if [ ! -d "$BASE_DIR" ]; then
    mkdir -p "$BASE_DIR"
    echo "_base/ created at $BASE_DIR"
fi

# ── 创建目录 ──

echo "[1/2] Creating directories..."
for dir in context knowledge repos; do
    if [ -d "$BIZ_DIR/$dir" ]; then
        echo "  $BIZ_DIR/$dir/ exists, skipping"
    else
        mkdir -p "$BIZ_DIR/$dir"
        echo "  $BIZ_DIR/$dir/ created"
    fi
done

# ── 生成模板文件（幂等） ──

echo "[2/2] Generating template files..."

write_template() {
    local target="$1"
    if [ -f "$target" ]; then
        echo "  $target exists, skipping"
        return
    fi
    # content is read from stdin
    cat > "$target"
    echo "  $target created ← please edit"
}

write_template "$BIZ_DIR/domain.yaml" <<EOF
name: $DOMAIN
display_name: $DOMAIN
description: TODO — 简要描述该业务域

repos:
  # - url: git@gitlab.company.com:team/repo-name.git
  #   branch: master
EOF

write_template "$BIZ_DIR/context/background.md" <<'EOF'
# 业务背景

<!-- TODO: 描述业务背景和发展历程 -->

# 核心概念

<!-- TODO: 定义业务领域中的核心概念和术语 -->
EOF

# ── Done ──

echo ""
echo "=== Domain $DOMAIN initialized ==="
echo ""
echo "Next steps:"
echo "  1. Edit $BIZ_DIR/domain.yaml    — fill repo URLs"
echo "  2. Edit $BIZ_DIR/context/       — add business context"
echo "  3. Run  ./scripts/clone-repos.sh $DOMAIN  — clone repos"
echo "  4. Use  generate-knowledge skill   — generate knowledge base"
