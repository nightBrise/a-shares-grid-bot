#!/bin/bash
# Dashboard 进程管理脚本
# 用法: ./dashboard_ctl.sh [start|stop|status|share]

PIDFILE="/tmp/dashboard.pid"
LOGFILE="/tmp/dashboard.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

start() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Dashboard 已在运行 (PID $(cat "$PIDFILE"))"
        return 1
    fi
    cd "$SCRIPT_DIR"
    nohup python -u dashboard.py "$@" > "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    # 等待启动，最多 30 秒
    for i in $(seq 1 30); do
        sleep 1
        if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "启动失败，查看日志: $LOGFILE"
            cat "$LOGFILE"
            return 1
        fi
        if grep -q "Running on local URL" "$LOGFILE" 2>/dev/null; then
            break
        fi
    done
    echo "Dashboard 已启动 (PID $(cat "$PIDFILE"))"
    echo "本地: http://127.0.0.1:7860"
    # 如果有 share 参数，等待公网链接生成
    if echo "$@" | grep -q "\-\-share"; then
        for i in $(seq 1 20); do
            SHARE_URL=$(grep -o "https://.*\.gradio\.live" "$LOGFILE" 2>/dev/null)
            if [ -n "$SHARE_URL" ]; then
                echo "公网: $SHARE_URL"
                return 0
            fi
            sleep 1
        done
        echo "公网链接生成中，请稍后用 status 查看"
    fi
}

stop() {
    # 杀掉 frpc 隧道子进程
    pkill -f "frpc_linux_amd64_v0.3" 2>/dev/null
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
            echo "Dashboard 已停止 (PID $PID)"
        else
            echo "进程 $PID 已不存在"
        fi
        rm -f "$PIDFILE"
    else
        PID=$(lsof -t -i:7860 2>/dev/null)
        if [ -n "$PID" ]; then
            kill "$PID"
            echo "Dashboard 已停止 (PID $PID)"
        else
            echo "Dashboard 未在运行"
        fi
    fi
}

status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        PID=$(cat "$PIDFILE")
        echo "Dashboard 运行中 (PID $PID)"
        echo "本地: http://127.0.0.1:7860"
        grep -o "https://.*\.gradio\.live" "$LOGFILE" 2>/dev/null
    else
        echo "Dashboard 未在运行"
    fi
}

case "${1:-}" in
    start)
        shift
        start
        ;;
    share)
        start --share
        ;;
    stop)
        stop
        ;;
    status)
        status
        ;;
    restart)
        stop
        sleep 1
        if [ "${2:-}" = "--share" ]; then
            start --share
        else
            start
        fi
        ;;
    *)
        echo "用法: $0 {start|share|stop|status|restart}"
        echo "  start    - 启动 dashboard（仅本地访问）"
        echo "  share    - 启动 dashboard（生成公网链接）"
        echo "  stop     - 停止 dashboard"
        echo "  status   - 查看运行状态"
        echo "  restart  - 重启（加 --share 生成公网链接）"
        ;;
esac
