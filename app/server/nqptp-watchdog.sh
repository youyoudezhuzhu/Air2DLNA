#!/bin/bash
# nqptp 看护脚本 —— 以 root 运行。
#
# 为什么需要 root：NQPTP 必须独占 UDP 319/320（特权端口），飞牛原生应用没有
# systemd 的 AmbientCapabilities 机制可用。因此只有这个 45KB 的单一职责守护进程
# 以 root 运行；shairport-sync 与 bridge.py 都以应用用户运行（见 cmd/main）。
#
# 本脚本由 cmd/main 以 nohup 方式启动，负责「启动 + 保持存活 + 退出清理」。
set -u

VAR="${TRIM_PKGVAR:-/vol1/@appdata/air2dlna}"
BIN="${TRIM_APPDEST:-/var/apps/air2dlna/target}/server/bin/nqptp"
PID_FILE="$VAR/nqptp.pid"
LOG="$VAR/nqptp.log"
MAIN_LOG="$VAR/main.log"
BRIDGE_STDERR_LOG="$VAR/bridge-stderr.log"
STOP_FILE="$VAR/nqptp-watchdog.stop"

# 日志上限与保留尾部（与 cmd/main、supervisor.py 保持一致）
LOG_MAX_BYTES="${LOG_MAX_BYTES:-5242880}"    # 5 MiB
LOG_KEEP_BYTES="${LOG_KEEP_BYTES:-1048576}"  # 1 MiB

log_msg() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" >> "$MAIN_LOG"
}

# 超限时原地保留尾部（不能 mv：nqptp 以 >> 持有该文件，mv 后写入会落到旧 inode）
trim_log() {
    local file="$1" size tmp
    [ -f "$file" ] || return 0
    size="$(stat -c %s "$file" 2>/dev/null || echo 0)"
    case "$size" in ''|*[!0-9]*) return 0 ;; esac
    [ "$size" -le "$LOG_MAX_BYTES" ] && return 0
    tmp="$file.rotate.tmp"
    if tail -c "$LOG_KEEP_BYTES" "$file" > "$tmp" 2>/dev/null; then
        if cat "$tmp" > "$file" 2>/dev/null; then
            log_msg "[logrotate] 日志超过 $((LOG_MAX_BYTES / 1048576))MB（原 $size 字节），已保留尾部 $((LOG_KEEP_BYTES / 1024))KB: $(basename "$file")"
        fi
    fi
    rm -f "$tmp" 2>/dev/null
    return 0
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
trim_log "$LOG"
start_nqptp

tick=0
while [ ! -e "$STOP_FILE" ]; do
    sleep 10
    tick=$((tick + 1))
    # 每 60 秒给「没有内置轮转」的日志收口一次：
    # nqptp.log（本脚本启动的进程）、main.log（生命周期）、bridge-stderr.log（bridge 的 stderr）
    if [ $((tick % 6)) -eq 0 ]; then
        trim_log "$LOG"
        trim_log "$MAIN_LOG"
        trim_log "$BRIDGE_STDERR_LOG"
    fi
    if ! is_running; then
        log_msg "检测到 nqptp 已退出，正在重启"
        start_nqptp
    fi
done

stop_nqptp
exit 0
