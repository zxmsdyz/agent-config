#!/usr/bin/env bash
# rc-disconnect-snapshot.sh — 手机端报 "disconnected" 时一键采证
#
# 验证假设 H4（server-side stale environment）需要的证据：
#   - bridge 进程是否还活？是否还在 poll？
#   - 最新 poll 距离现在多久？有没有 401 / token / shutdown / close？
#   - environment_id 还是不是开机那个？（换了 = bridge 自己已经 deregister + re-register）
#   - 当前 TCP 出口连接是否健康？
#
# ⚠️ 重要：报断后 *先跑这个脚本，再* 重启 rc-debug。否则证据丢失。
#
# 用法：
#   ~/.claude/bin/rc-disconnect-snapshot.sh                          # 自动找最新 debug log
#   ~/.claude/bin/rc-disconnect-snapshot.sh "手机说 disconnected"     # 附一句话备注
#   ~/.claude/bin/rc-disconnect-snapshot.sh --log /path/to/specific.log "备注"

set -u

NOTE=""
LOG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --log) LOG="$2"; shift 2 ;;
    --help|-h)
      grep -E '^#' "$0" | sed 's/^# \?//' | head -25
      exit 0
      ;;
    *) NOTE="$1"; shift ;;
  esac
done

if [[ -z "$LOG" ]]; then
  LOG=$(ls -t ~/.claude/logs/rc-debug/rc-*.debug.log 2>/dev/null | head -1)
fi

if [[ ! -f "$LOG" ]]; then
  echo "ERROR: no debug log found in ~/.claude/logs/rc-debug/" >&2
  exit 1
fi

SNAP_DIR=~/.claude/logs/rc-debug/disconnect-snapshots
mkdir -p "$SNAP_DIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$SNAP_DIR/snap-${TS}.log"

# 找 claude remote-control 进程（可能 0/1 个）
RC_PID=$(pgrep -f 'claude remote-control' | head -1 || true)

{
  echo "===== rc-disconnect-snapshot @ $(date -Iseconds) ====="
  echo "user_note: ${NOTE:-<none>}"
  echo "debug_log: $LOG"
  echo
  echo "## bridge 进程"
  if [[ -n "$RC_PID" ]]; then
    ps -p "$RC_PID" -o pid,ppid,stat,etime,time,wchan:30,cmd 2>&1
    echo
    echo "thread states:"
    for tid in $(ls /proc/$RC_PID/task/ 2>/dev/null); do
      state=$(awk '/^State:/ {print $2}' /proc/$RC_PID/task/$tid/status 2>/dev/null)
      wchan=$(cat /proc/$RC_PID/task/$tid/wchan 2>/dev/null)
      name=$(awk '/^Name:/ {print $2}' /proc/$RC_PID/task/$tid/status 2>/dev/null)
      printf "  tid=%s name=%-15s state=%s wchan=%s\n" "$tid" "$name" "$state" "$wchan"
    done
  else
    echo "ERROR: no claude remote-control process found (rc-debug 已退出)"
  fi
  echo

  echo "## TCP 出口（claude 进程）"
  ss -tnpo 2>/dev/null | awk 'NR==1 || /claude/'
  echo

  echo "## bridge log 摘要"
  total=$(wc -l < "$LOG")
  polls=$(grep -c 'consecutive empty polls' "$LOG")
  reconnects=$(grep -c 'Reconnected after' "$LOG")
  echo "  总行数=$total  empty_poll=$polls  reconnect=$reconnects"
  echo "  environment_id: $(grep -oE 'environmentId=env_[A-Za-z0-9]+' "$LOG" | tail -1)"
  echo "  日志首行时间:   $(head -1 "$LOG" | awk '{print $1}')"
  echo "  日志末行时间:   $(tail -1 "$LOG" | awk '{print $1}')"
  last_ts=$(tail -1 "$LOG" | awk '{print $1}' | sed 's/Z$//')
  if [[ -n "$last_ts" ]]; then
    last_epoch=$(date -d "$last_ts" +%s 2>/dev/null || echo 0)
    now_epoch=$(date -u +%s)
    if [[ $last_epoch -gt 0 ]]; then
      gap=$((now_epoch - last_epoch))
      echo "  距离最后一行: ${gap}s"
      if [[ $gap -gt 60 ]]; then
        echo "  ⚠️ 最后一行超过 60s 没新行，poll 可能卡了 / 进程冻了"
      fi
    fi
  fi
  echo

  echo "## bridge log 最近 30 行"
  tail -30 "$LOG"
  echo

  echo "## bridge log 中可疑事件（401 / 403 / close / token / shutdown / disconnect / error / reconnect）"
  grep -nE '401|403|close|token|shutdown|disconnect|[Ee]rror|Reconnected|[Tt]imeout|EPIPE|ECONNRESET' "$LOG" | tail -40 || echo "  (no suspicious events)"
  echo

  echo "## 网络代理 / DNS"
  echo "  http_proxy=${http_proxy:-<unset>}"
  echo "  https_proxy=${https_proxy:-<unset>}"
  echo "  all_proxy=${all_proxy:-<unset>}"
  echo "  api.anthropic.com -> $(getent hosts api.anthropic.com 2>&1 | head -1)"
  echo

  echo "## sysctl keepalive（应为 120/30/3）"
  sysctl net.ipv4.tcp_keepalive_time net.ipv4.tcp_keepalive_intvl net.ipv4.tcp_keepalive_probes 2>&1
  echo

  echo "## 系统时间"
  echo "  UTC: $(date -u)"
  echo "  Local: $(date)"
  echo "  WSL uptime: $(uptime)"

  echo
  echo "===== 快照结束 ====="
} > "$OUT"

# 屏幕回显摘要
echo "快照已写：$OUT"
echo
echo "===== 关键指标速览 ====="
grep -E '^  (总行数|环境|environment_id|日志末行时间|距离最后一行|⚠️|http_proxy|UTC|Local)' "$OUT" | head -15
echo
echo "下一步："
echo "  1. 现在手机端再试一次 → 看 host log 是否新增 poll/work 事件："
echo "       tail -F $LOG"
echo "  2. 如果 5min 内 host log 完全无新行 → bridge 假死，验证 H4"
echo "  3. 然后再重启 rc-debug：先 SIGINT，等 deregister 成功的 200 OK，再启动新 session"
