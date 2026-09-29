#!/bin/bash
# 行为测试：从 cmd/main 里提取真实的 trim_log 函数，验证其行为。
#
# 关键回归点：日志轮转必须**原地重写**（保留 inode）。写入方（bridge 的
# stdout/stderr、watchdog 的 >>）以 O_APPEND 持有文件描述符；如果实现改成
# mv/rename，它们会继续写进已改名的旧 inode，新文件永远收不到新日志。
#
# 用法：bash tests/test_logrotate_shell.sh
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0

ok()   { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 $3，实际 $2）"; fi; }

# 从 cmd/main 提取 trim_log 函数定义，确保测的是真代码而不是副本
sed -n '/^trim_log() {/,/^}$/p' "$ROOT/cmd/main" > "$TMP/trim_log.sh"
if [ ! -s "$TMP/trim_log.sh" ]; then
    echo "  FAIL  未能从 cmd/main 提取 trim_log 函数"
    exit 1
fi
echo "  INFO  已从 cmd/main 提取 trim_log（$(wc -l < "$TMP/trim_log.sh") 行）"

LOG_MAX_BYTES=1048576      # 1 MiB
LOG_KEEP_BYTES=131072      # 128 KiB
LOG_FILE="$TMP/test.log"
MAIN_STUB="$TMP/main-stub.log"

log_msg() { echo "$(date '+%F %T') - $*" >> "$MAIN_STUB"; }
# shellcheck disable=SC1090
source "$TMP/trim_log.sh"

echo "[1] 限额内的文件不动"
head -c 2048 /dev/zero | tr '\0' 'a' > "$LOG_FILE"
size_before=$(stat -c %s "$LOG_FILE")
trim_log "$LOG_FILE"
check "小文件保持原样" "$(stat -c %s "$LOG_FILE")" "$size_before"

echo "[2] 超过上限时保留尾部"
{ head -c 2000000 /dev/zero | tr '\0' 'a'; echo; echo "TAIL-MARKER-9999"; } > "$LOG_FILE"
size_before=$(stat -c %s "$LOG_FILE")
trim_log "$LOG_FILE"
size_after=$(stat -c %s "$LOG_FILE")
if [ "$size_after" -lt "$size_before" ] && [ "$size_after" -le $((LOG_KEEP_BYTES + 512)) ]; then
    ok "文件已收缩到尾部量级（$size_before -> $size_after）"
else
    bad "收缩异常（$size_before -> $size_after）"
fi
if tail -c 200 "$LOG_FILE" | grep -q 'TAIL-MARKER-9999'; then
    ok "尾部内容保留"
else
    bad "尾部内容丢失"
fi
if grep -q 'logrotate' "$MAIN_STUB" 2>/dev/null; then
    ok "写入轮转记录到主日志"
else
    bad "未记录轮转事件"
fi

echo "[3] 轮转后 inode 不变（原地重写）"
{ head -c 2000000 /dev/zero | tr '\0' 'b'; } > "$LOG_FILE"
inode_before=$(stat -c %i "$LOG_FILE")
trim_log "$LOG_FILE"
check "inode 保持不变" "$(stat -c %i "$LOG_FILE")" "$inode_before"

echo "[4] 轮转后仍有 O_APPEND 写入方（子进程 stdout / shell >>）"
: > "$LOG_FILE"
exec 3>>"$LOG_FILE"                      # 模拟子进程以 O_APPEND 持有的 fd
printf 'written-before-rotation\n' >&3
{ head -c 2000000 /dev/zero | tr '\0' 'c'; } >> "$LOG_FILE"
trim_log "$LOG_FILE"
printf 'written-after-rotation\n' >&3
exec 3>&-
if grep -q 'written-after-rotation' "$LOG_FILE"; then
    ok "轮转后 O_APPEND 写入仍落在同一文件"
else
    bad "轮转后写入丢失（说明实现用了 mv/rename）"
fi

echo "[5] 不存在的文件不报错"
trim_log "$TMP/does-not-exist.log" >/dev/null 2>&1
check "缺失文件返回正常" "$?" "0"

echo
echo "日志轮转行为测试: $PASS 通过 / $FAIL 失败"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
