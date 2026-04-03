#!/usr/bin/env bash
#
# 读取 domain.yaml 中的 repos 列表，clone 到 biz/<domain>/repos/
#
# 用法：
#   ./scripts/clone-repos.sh <domain>       # clone 单个域的 repos
#   ./scripts/clone-repos.sh --all          # clone 所有域的 repos
#   ./scripts/clone-repos.sh <domain> --dry-run   # 预览
#
# 环境：  BIZ_BASE_PATH=<path>  覆盖默认 biz/ 目录（同 core/config.py）
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
source "$SCRIPT_DIR/env.sh"

# ── Python（需要 PyYAML）──

PYTHON="python3"
if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
fi

# ── 参数解析 ──

DOMAIN=""
ALL=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)     ALL=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) echo "Usage: $0 <domain> [--dry-run]"; echo "       $0 --all [--dry-run]"; exit 0 ;;
        -*)        echo "Unknown option: $1" >&2; exit 1 ;;
        *)         DOMAIN="$1"; shift ;;
    esac
done

if [[ "$ALL" = false && -z "$DOMAIN" ]]; then
    echo "Error: specify a domain name or --all" >&2
    echo "Usage: $0 <domain> [--dry-run]" >&2
    echo "       $0 --all [--dry-run]" >&2
    exit 1
fi

# ── 解析 domain.yaml 中的 repos ──

parse_repos() {
    local yaml_file="$1"
    $PYTHON -c "
import json, sys
try:
    import yaml
except ImportError:
    print('Error: PyYAML not installed. Run: pip install pyyaml', file=sys.stderr)
    sys.exit(1)
with open('$yaml_file') as f:
    data = yaml.safe_load(f)
repos = data.get('repos') or []
for r in repos:
    if isinstance(r, dict) and r.get('url'):
        print(json.dumps(r))
"
}

# ── clone 单个域 ──

clone_domain() {
    local domain="$1"
    local yaml_file="$BIZ_BASE/$domain/domain.yaml"
    local repos_dir="$BIZ_BASE/$domain/repos"

    if [[ ! -f "$yaml_file" ]]; then
        echo "  SKIP: $yaml_file not found"
        return
    fi

    mkdir -p "$repos_dir"

    local count=0
    while IFS= read -r repo_json; do
        local url branch repo_name
        url=$(echo "$repo_json" | $PYTHON -c "import json,sys; print(json.load(sys.stdin)['url'])")
        branch=$(echo "$repo_json" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('branch',''))")
        # Extract repo name from URL: git@host:team/repo.git → repo
        repo_name=$(basename "$url" .git)

        if [[ -d "$repos_dir/$repo_name" ]]; then
            echo "  $repo_name/ exists, skipping (use git -C $repos_dir/$repo_name pull to update)"
            count=$((count + 1))
            continue
        fi

        local clone_args=(git clone)
        if [[ -n "$branch" ]]; then
            clone_args+=(--branch "$branch")
        fi
        clone_args+=("$url" "$repos_dir/$repo_name")

        if $DRY_RUN; then
            echo "  [dry-run] ${clone_args[*]}"
        else
            echo "  Cloning $repo_name..."
            "${clone_args[@]}"
        fi
        count=$((count + 1))
    done < <(parse_repos "$yaml_file")

    if [[ $count -eq 0 ]]; then
        echo "  No repos defined in $yaml_file"
    fi
}

# ── 主逻辑 ──

if $ALL; then
    echo "=== Clone Repos: all domains ==="
    echo ""
    for domain_dir in "$BIZ_BASE"/*/; do
        d=$(basename "$domain_dir")
        # 跳过 _base 等特殊目录
        [[ "$d" == _* || "$d" == .* ]] && continue
        echo "[$d]"
        clone_domain "$d"
        echo ""
    done
else
    if [[ ! -d "$BIZ_BASE/$DOMAIN" ]]; then
        echo "Error: domain directory $BIZ_BASE/$DOMAIN/ not found" >&2
        echo "Run ./scripts/init-domain.sh $DOMAIN first" >&2
        exit 1
    fi
    echo "=== Clone Repos: $DOMAIN ==="
    echo ""
    clone_domain "$DOMAIN"
fi

echo "Done."
