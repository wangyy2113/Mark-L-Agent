#!/usr/bin/env bash
#
# Mark-L Agent — 守护脚本（无需 root 权限）
#
# ⚠ 已弃用：现在用 systemd 管理服务，此脚本仅保留备用。
#   参见 scripts/mark-l-agent.service
#
# 用法：
#   ./scripts/run.sh           # 前台运行（带崩溃自动重启）
#   nohup ./scripts/run.sh &   # 后台运行
#
# 停止：
#   ./scripts/run.sh stop      # 发送 SIGTERM，触发 graceful shutdown
#
# 开机自启（可选）：
#   crontab -e
#   @reboot cd /path/to/mark-l-agent && nohup ./scripts/run.sh >> data/run.log 2>&1 &

set -euo pipefail
cd "$(dirname "$0")/.."

PID_FILE="data/mark-l-agent.pid"
LOG_FILE="data/run.log"

# ── stop 命令 ──

if [ "${1:-}" = "stop" ]; then
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping mark-l-agent (pid $pid)..."
            kill "$pid"
            # 等待进程退出，最多 30 秒
            for i in $(seq 1 30); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "Process didn't exit, sending SIGKILL..."
                kill -9 "$pid"
            fi
            echo "Stopped."
        else
            echo "Process $pid not running, cleaning up pid file."
        fi
        rm -f "$PID_FILE"
    else
        echo "No pid file found, is mark-l-agent running?"
    fi
    exit 0
fi

# ── 防止重复启动 ──

if [ -f "$PID_FILE" ]; then
    old_pid=$(cat "$PID_FILE")
    if kill -0 "$old_pid" 2>/dev/null; then
        echo "ERROR: mark-l-agent already running (pid $old_pid)" >&2
        echo "  Run: ./scripts/run.sh stop" >&2
        exit 1
    fi
    rm -f "$PID_FILE"
fi

# ── 环境准备 ──

mkdir -p data
source .venv/bin/activate

# ── 守护循环 ──

cleanup() {
    rm -f "$PID_FILE"
    exit 0
}
trap cleanup SIGTERM SIGINT

echo "[$(date)] run.sh started" >> "$LOG_FILE"

while true; do
    echo "[$(date)] Starting mark-l-agent..." >> "$LOG_FILE"
    python main.py &
    child=$!
    echo "$child" > "$PID_FILE"

    # 等待子进程退出，同时保持 trap 可响应
    wait "$child" || true
    exit_code=$?
    rm -f "$PID_FILE"

    echo "[$(date)] Process exited (code=$exit_code), restarting in 5s..." >> "$LOG_FILE"
    sleep 5
done
