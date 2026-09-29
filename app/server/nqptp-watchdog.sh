#!/bin/bash
# nqptp 看护脚本 —— 以 root 运行。
#
# 为什么需要 root：NQPTP 必须独占 UDP 319/320（特权端口），飞牛原生应用没有
# systemd 的 AmbientCapabilities 机制可用。因此只有这个 45KB 的单一职责守护进程
# 以 root 运行；shairport-sync 与 bridge.py 都以应用用户运行（见 cmd/main）。
#
# 本脚本由 cmd/main 以 nohup 方式启动，负责「启动 + 保持存活 + 退出清理」。
set -u

VAR="${TRIM_PKGVAR:-/vol1/@appdata/airplay2dlna}"
BIN="${TRIM_APPDEST:-/var/apps/airplay2dlna/target}/server/bin/nqptp"
PID_FILE="$VAR/nqptp.pid"
LOG="$VAR/nqptp.log"
MAIN_LOG="$VAR/main.log"
STOP_FILE="$VAR/nqptp-watchdog.stop"

log_msg() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" >> "$MAIN_LOG"
}

is_running() {
    [ -r "$PID_FILE" ] || return 1
    local pid
    pid=$(head -n 1 "$PID_FILE" 2>/dev/null | tr -d '[:space:]')
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

start_nqptp() {
    if is_running; then
        return 0
    fi
    if [ ! -x "$BIN" ]; then
        log_msg "nqptp 可执行文件不存在或不可执行: $BIN"
        return 1
    fi
    "$BIN" >>"$LOG" 2>&1 &
    echo "$!" > "$PID_FILE"
    log_msg "nqptp 已启动 (pid=$!)"
    return 0
}

stop_nqptp() {
    if [ -r "$PID_FILE" ]; then
        local pid
        pid=$(head -n 1 "$PID_FILE" 2>/dev/null | tr -d '[:space:]')
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null
            log_msg "nqptp 已停止 (pid=$pid)"
        fi
        rm -f "$PID_FILE"
    fi
}

mkdir -p "$VAR"
rm -f "$STOP_FILE"
start_nqptp

while [ ! -e "$STOP_FILE" ]; do
    sleep 10
    if ! is_running; then
        log_msg "检测到 nqptp 已退出，正在重启"
        start_nqptp
    fi
done

stop_nqptp
exit 0
