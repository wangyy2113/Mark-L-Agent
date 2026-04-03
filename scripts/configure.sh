#!/usr/bin/env bash
#
# Mark-L Agent — 交互式配置编辑脚本
#
# 用法：
#   configure.sh                          查看当前 .env 配置（敏感值脱敏）
#   configure.sh init                     首次部署向导（必填 + 常用项）
#   configure.sh edit [section]           交互式编辑某组 (feishu|claude|lite|admin|paths|session|all)
#   configure.sh set KEY=VALUE ...        直接设值（支持多个，无交互）
#   configure.sh get KEY                  查看单个值（明文，方便复制）

set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
ENV_EXAMPLE=".env.example"
MCP_FILE="mcp.json"
MCP_EXAMPLE="mcp.json.example"

# ── .env 分组定义 ──

declare -a SECTION_NAMES=(feishu claude lite admin paths session)

declare -a KEYS_feishu=(FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_VERIFICATION_TOKEN FEISHU_ENCRYPT_KEY FEISHU_PERSONAL_TOKEN FEISHU_PROJECT_TOKEN)
declare -a KEYS_claude=(ANTHROPIC_API_KEY ANTHROPIC_BASE_URL CLAUDE_MODEL)
declare -a KEYS_lite=(LLM_PROVIDER LITE_API_KEY LITE_BASE_URL LITE_MODEL)
declare -a KEYS_admin=(ADMIN_OPEN_ID BOT_OPEN_ID)
declare -a KEYS_paths=(BIZ_BASE_PATH MCP_CONFIG_PATH SESSION_DB_PATH FEISHU_DOC_FOLDER_TOKEN)
declare -a KEYS_session=(SESSION_TTL_SECONDS)

# 必填项（init 向导必问）
declare -a REQUIRED_KEYS=(FEISHU_APP_ID FEISHU_APP_SECRET ANTHROPIC_API_KEY)

# 常用可选（init 也问）
declare -a COMMON_KEYS=(ANTHROPIC_BASE_URL ADMIN_OPEN_ID BIZ_BASE_PATH)

# 敏感 key 模式（脱敏显示）
SENSITIVE_PATTERN="SECRET|KEY|TOKEN|ENCRYPT"

# ── 颜色 ──

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    CYAN='\033[0;36m'
    DIM='\033[2m'
    BOLD='\033[1m'
    RESET='\033[0m'
else
    RED='' GREEN='' YELLOW='' CYAN='' DIM='' BOLD='' RESET=''
fi

# ── 工具函数 ──

# 读取 .env 中某个 key 的值（不 source，纯文本解析）
_get_value() {
    local key="$1"
    if [[ ! -f "$ENV_FILE" ]]; then
        return 1
    fi
    # 匹配未注释的 KEY=VALUE 行
    while IFS= read -r line; do
        # 跳过注释和空行
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue
        # 匹配 KEY=VALUE（值可能含 = 号）
        if [[ "$line" =~ ^[[:space:]]*${key}=(.*) ]]; then
            echo "${BASH_REMATCH[1]}"
            return 0
        fi
    done < "$ENV_FILE"
    return 1
}

# 判断 key 是否敏感
_is_sensitive() {
    local key="$1"
    [[ "$key" =~ $SENSITIVE_PATTERN ]]
}

# 脱敏显示
_mask_value() {
    local value="$1"
    local len=${#value}
    if [[ $len -le 4 ]]; then
        echo "****"
    elif [[ $len -le 8 ]]; then
        echo "${value:0:2}****"
    else
        echo "${value:0:4}****${value: -4}"
    fi
}

# 写入/更新 .env 中的 key
# 三种情况：
#   1. KEY=old 存在（未注释）→ 替换 value
#   2. # KEY=placeholder 存在（已注释）→ 取消注释并设值
#   3. 都不存在 → 追加到文件末尾
_set_value() {
    local key="$1" value="$2"
    local tmpfile
    tmpfile=$(mktemp)
    local found=false

    if [[ ! -f "$ENV_FILE" ]]; then
        echo "${key}=${value}" > "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        return 0
    fi

    while IFS= read -r line || [[ -n "$line" ]]; do
        # 匹配未注释的 KEY=...
        if [[ "$line" =~ ^[[:space:]]*${key}= ]]; then
            echo "${key}=${value}" >> "$tmpfile"
            found=true
        # 匹配已注释的 # KEY=...
        elif [[ "$line" =~ ^[[:space:]]*#[[:space:]]*${key}= ]] && [[ "$found" == false ]]; then
            echo "${key}=${value}" >> "$tmpfile"
            found=true
        else
            echo "$line" >> "$tmpfile"
        fi
    done < "$ENV_FILE"

    if [[ "$found" == false ]]; then
        echo "${key}=${value}" >> "$tmpfile"
    fi

    mv "$tmpfile" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

# 获取 section 的所有 key
_get_section_keys() {
    local section="$1"
    local var="KEYS_${section}[@]"
    echo "${!var}"
}

# 显示单个 key 的值（带脱敏）
_display_key() {
    local key="$1"
    local value
    if value=$(_get_value "$key" 2>/dev/null); then
        if [[ -z "$value" ]]; then
            printf "  %-30s ${DIM}(空)${RESET}\n" "$key"
        elif _is_sensitive "$key"; then
            printf "  %-30s %s\n" "$key" "$(_mask_value "$value")"
        else
            printf "  %-30s %s\n" "$key" "$value"
        fi
    else
        printf "  %-30s ${DIM}(未设置)${RESET}\n" "$key"
    fi
}

# ── show 命令（默认） ──

cmd_show() {
    if [[ ! -f "$ENV_FILE" ]]; then
        echo -e "${YELLOW}未找到 .env 文件。运行 ${BOLD}configure.sh init${RESET}${YELLOW} 创建。${RESET}"
        exit 1
    fi

    echo -e "${BOLD}Mark-L Agent 配置${RESET}  ${DIM}($ENV_FILE)${RESET}"
    echo ""

    for section in "${SECTION_NAMES[@]}"; do
        echo -e "${CYAN}[$section]${RESET}"
        for key in $(_get_section_keys "$section"); do
            _display_key "$key"
        done
        echo ""
    done

    echo -e "${DIM}提示：敏感值已脱敏。用 ${RESET}configure.sh get KEY${DIM} 查看明文。${RESET}"
}

# ── get 命令 ──

cmd_get() {
    local key="$1"
    if [[ ! -f "$ENV_FILE" ]]; then
        echo -e "${YELLOW}未找到 .env 文件${RESET}" >&2
        exit 1
    fi
    local value
    if value=$(_get_value "$key" 2>/dev/null); then
        echo "$value"
    else
        echo -e "${YELLOW}${key} 未设置${RESET}" >&2
        exit 1
    fi
}

# ── set 命令 ──

cmd_set() {
    local changed=0
    for arg in "$@"; do
        if [[ "$arg" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            local key="${BASH_REMATCH[1]}"
            local value="${BASH_REMATCH[2]}"
            _set_value "$key" "$value"
            echo -e "  ${GREEN}✓${RESET} ${key}=${value}"
            changed=$((changed + 1))
        else
            echo -e "  ${RED}✗${RESET} 格式错误: $arg  (应为 KEY=VALUE)" >&2
        fi
    done
    if [[ $changed -gt 0 ]]; then
        echo ""
        echo -e "${YELLOW}需要重启服务才能生效。${RESET}"
    fi
}

# ── edit 命令 ──

cmd_edit() {
    local section="${1:-all}"

    if [[ ! -f "$ENV_FILE" ]]; then
        echo -e "${YELLOW}未找到 .env 文件。运行 ${BOLD}configure.sh init${RESET}${YELLOW} 先创建。${RESET}"
        exit 1
    fi

    local sections=()
    if [[ "$section" == "all" ]]; then
        sections=("${SECTION_NAMES[@]}")
    else
        # 验证 section 名称
        local valid=false
        for s in "${SECTION_NAMES[@]}"; do
            if [[ "$s" == "$section" ]]; then
                valid=true
                break
            fi
        done
        if [[ "$valid" == false ]]; then
            echo -e "${RED}未知分组: $section${RESET}"
            echo "可用分组: ${SECTION_NAMES[*]} all"
            exit 1
        fi
        sections=("$section")
    fi

    local -a changes=()

    for sec in "${sections[@]}"; do
        echo -e "${CYAN}[$sec]${RESET}"
        for key in $(_get_section_keys "$sec"); do
            local current=""
            local display=""
            if current=$(_get_value "$key" 2>/dev/null); then
                if _is_sensitive "$key" && [[ -n "$current" ]]; then
                    display="$(_mask_value "$current")"
                else
                    display="$current"
                fi
            fi

            if [[ -n "$display" ]]; then
                printf "  %s [%s]: " "$key" "$display"
            else
                printf "  %s: " "$key"
            fi
            read -r input
            if [[ -n "$input" ]]; then
                changes+=("${key}=${input}")
            fi
        done
        echo ""
    done

    if [[ ${#changes[@]} -eq 0 ]]; then
        echo "无变更。"
        return
    fi

    echo -e "${BOLD}变更摘要：${RESET}"
    for change in "${changes[@]}"; do
        local k="${change%%=*}"
        local v="${change#*=}"
        if _is_sensitive "$k"; then
            echo -e "  ${k} = $(_mask_value "$v")"
        else
            echo -e "  ${k} = ${v}"
        fi
    done

    echo ""
    printf "确认写入？[Y/n] "
    read -r confirm
    if [[ "$confirm" =~ ^[Nn] ]]; then
        echo "已取消。"
        return
    fi

    for change in "${changes[@]}"; do
        local k="${change%%=*}"
        local v="${change#*=}"
        _set_value "$k" "$v"
    done

    echo -e "${GREEN}已写入 ${#changes[@]} 项配置。${RESET}"
    echo -e "${YELLOW}需要重启服务才能生效。${RESET}"
}

# ── init 命令 ──

cmd_init() {
    echo -e "${BOLD}Mark-L Agent 首次部署向导${RESET}"
    echo ""

    # 1. 确保 .env 存在
    if [[ ! -f "$ENV_FILE" ]]; then
        if [[ -f "$ENV_EXAMPLE" ]]; then
            cp "$ENV_EXAMPLE" "$ENV_FILE"
            chmod 600 "$ENV_FILE"
            echo -e "  ${GREEN}✓${RESET} 已从 $ENV_EXAMPLE 创建 $ENV_FILE"
        else
            touch "$ENV_FILE"
            chmod 600 "$ENV_FILE"
            echo -e "  ${GREEN}✓${RESET} 已创建空 $ENV_FILE"
        fi
    else
        echo -e "  ${DIM}$ENV_FILE 已存在，在此基础上配置${RESET}"
    fi
    echo ""

    local -a changes=()

    # 2. 必填项
    echo -e "${CYAN}必填配置${RESET}"
    for key in "${REQUIRED_KEYS[@]}"; do
        local current=""
        local display=""
        if current=$(_get_value "$key" 2>/dev/null); then
            if _is_sensitive "$key" && [[ -n "$current" ]]; then
                display="$(_mask_value "$current")"
            else
                display="$current"
            fi
        fi

        while true; do
            if [[ -n "$display" ]]; then
                printf "  %s [%s]: " "$key" "$display"
            else
                printf "  %s: " "$key"
            fi
            read -r input
            if [[ -n "$input" ]]; then
                changes+=("${key}=${input}")
                break
            elif [[ -n "$current" ]]; then
                # 回车保持当前值
                break
            else
                echo -e "    ${RED}此项必填${RESET}"
            fi
        done
    done
    echo ""

    # 3. 常用可选项
    echo -e "${CYAN}常用配置（可选，回车跳过）${RESET}"
    for key in "${COMMON_KEYS[@]}"; do
        local current=""
        local display=""
        if current=$(_get_value "$key" 2>/dev/null); then
            if _is_sensitive "$key" && [[ -n "$current" ]]; then
                display="$(_mask_value "$current")"
            else
                display="$current"
            fi
        fi

        if [[ -n "$display" ]]; then
            printf "  %s [%s]: " "$key" "$display"
        else
            printf "  %s: " "$key"
        fi
        read -r input
        if [[ -n "$input" ]]; then
            changes+=("${key}=${input}")
        fi
    done
    echo ""

    # 4. 写入变更
    if [[ ${#changes[@]} -gt 0 ]]; then
        echo -e "${BOLD}变更摘要：${RESET}"
        for change in "${changes[@]}"; do
            local k="${change%%=*}"
            local v="${change#*=}"
            if _is_sensitive "$k"; then
                echo -e "  ${GREEN}✓${RESET} ${k} = $(_mask_value "$v")"
            else
                echo -e "  ${GREEN}✓${RESET} ${k} = ${v}"
            fi
        done
        echo ""

        for change in "${changes[@]}"; do
            local k="${change%%=*}"
            local v="${change#*=}"
            _set_value "$k" "$v"
        done
        echo -e "${GREEN}已写入 .env${RESET}"
    else
        echo "无变更。"
    fi

    # 5. mcp.json 自动生成
    echo ""
    if [[ ! -f "$MCP_FILE" ]]; then
        if [[ -f "$MCP_EXAMPLE" ]]; then
            local app_id app_secret
            app_id=$(_get_value "FEISHU_APP_ID" 2>/dev/null) || app_id=""
            app_secret=$(_get_value "FEISHU_APP_SECRET" 2>/dev/null) || app_secret=""

            if [[ -n "$app_id" && -n "$app_secret" && "$app_id" != "cli_xxxxxxxxxxxx" ]]; then
                sed -e "s/YOUR_APP_ID/${app_id}/g" \
                    -e "s/YOUR_APP_SECRET/${app_secret}/g" \
                    "$MCP_EXAMPLE" > "$MCP_FILE"
                chmod 600 "$MCP_FILE"
                echo -e "  ${GREEN}✓${RESET} 已从模板生成 $MCP_FILE（飞书凭证已填入 lark-mcp 配置）"
                echo -e "  ${DIM}  提示：mcp.json 中的占位符会在启动时自动替换为 .env 中的值${RESET}"
            else
                cp "$MCP_EXAMPLE" "$MCP_FILE"
                chmod 600 "$MCP_FILE"
                echo -e "  ${YELLOW}!${RESET} 已复制 $MCP_FILE 模板（飞书凭证未填写，需手动编辑）"
            fi
        else
            echo -e "  ${DIM}未找到 $MCP_EXAMPLE，跳过 mcp.json 生成${RESET}"
        fi
    else
        echo -e "  ${DIM}$MCP_FILE 已存在，跳过${RESET}"
    fi

    echo ""
    echo -e "${BOLD}配置完成！${RESET}"
    echo ""
    echo "后续步骤："
    echo "  1. 检查配置:  ./scripts/configure.sh"
    echo "  2. 编辑 MCP:  vim mcp.json  (如需自定义)"
    echo "  3. 启动服务:  source .venv/bin/activate && python main.py"
    echo ""
    echo -e "${YELLOW}需要重启服务才能生效。${RESET}"
}

# ── 帮助 ──

cmd_help() {
    cat <<'USAGE'
Mark-L Agent 配置工具

用法：
  configure.sh                          查看当前配置（敏感值脱敏）
  configure.sh init                     首次部署向导
  configure.sh edit [section]           交互式编辑某组
  configure.sh set KEY=VALUE ...        直接设值
  configure.sh get KEY                  查看单个值（明文）
  configure.sh help                     显示此帮助

分组：
  feishu    飞书凭证 (APP_ID, APP_SECRET, VERIFICATION_TOKEN, ENCRYPT_KEY)
  claude    Claude API (API_KEY, BASE_URL, MODEL)
  lite      Lite 模式 (PROVIDER, API_KEY, BASE_URL, MODEL)
  admin     管理员 (ADMIN_OPEN_ID, BOT_OPEN_ID)
  paths     路径 (BIZ_BASE_PATH, MCP_CONFIG_PATH, SESSION_DB_PATH, DOC_FOLDER_TOKEN)
  session   会话 (SESSION_TTL_SECONDS)
  all       全部分组

示例：
  configure.sh set CLAUDE_MODEL=claude-sonnet-4-20250514
  configure.sh get ANTHROPIC_API_KEY
  configure.sh edit claude
  configure.sh edit all
USAGE
}

# ── 入口 ──

case "${1:-}" in
    ""|show)
        cmd_show
        ;;
    init)
        cmd_init
        ;;
    edit)
        cmd_edit "${2:-all}"
        ;;
    set)
        shift
        if [[ $# -eq 0 ]]; then
            echo "用法: configure.sh set KEY=VALUE [KEY=VALUE ...]" >&2
            exit 1
        fi
        cmd_set "$@"
        ;;
    get)
        if [[ -z "${2:-}" ]]; then
            echo "用法: configure.sh get KEY" >&2
            exit 1
        fi
        cmd_get "$2"
        ;;
    help|--help|-h)
        cmd_help
        ;;
    *)
        echo -e "${RED}未知命令: $1${RESET}" >&2
        echo "运行 configure.sh help 查看用法" >&2
        exit 1
        ;;
esac
