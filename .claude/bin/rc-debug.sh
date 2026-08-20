#!/usr/bin/env bash
# rc-debug.sh — 一键带 debug + TCP 监控启动 claude remote-control
#
# 用途：排查手机端 remote control 一段时间后所有窗口 disconnect 的问题
#   - 用官方 --debug-file 抓 websocket / 内部事件
#   - 后台每 5s 抓 ss 看 TCP 连接状态 + keepalive timer
#   - Ctrl+C 退出自动出 summary（断开关键事件 + TCP 连接数时间序列 + 归类口诀）
#
# 用法：
#   rc-debug.sh                  # 默认 session 名（hostname）
#   rc-debug.sh phone-debug      # 自定义 session 名

set -euo pipefail

LOG_DIR="${HOME}/.claude/logs/rc-debug"
TS="$(date +%Y%m%d-%H%M%S)"
DEBUG_LOG="${LOG_DIR}/rc-${TS}.debug.log"
TCP_LOG="${LOG_DIR}/rc-${TS}.tcp.log"
SUMMARY_LOG="${LOG_DIR}/rc-${TS}.summary.log"
SESSION_NAME="${1:-}"

mkdir -p "${LOG_DIR}"

cat <<EOF
=== claude remote-control debug session ===
Start time : $(date -Iseconds)
Debug log  : ${DEBUG_LOG}
TCP log    : ${TCP_LOG}
Summary    : ${SUMMARY_LOG}  (Ctrl+C 退出时生成)
Proxy env  : http_proxy=${http_proxy:-<unset>}
             all_proxy=${all_proxy:-<unset>}

实时跟进（另开 terminal）：
  tail -F ${DEBUG_LOG}
  tail -F ${TCP_LOG}

断开瞬间 grep（同上另开 terminal）：
  grep -iE 'close|1006|1002|ECONNRESET|reconnect' ${DEBUG_LOG}

Ctrl+C 退出会自动生成 summary。
----------------------------------------
EOF

# 后台 TCP watcher（每 5s 抓一次，含 keepalive timer）
(
  while true; do
    {
      printf '[%s]\n' "$(date +%H:%M:%S)"
      ss -tnpo 2>/dev/null | grep claude | grep -v grep || echo "  (no claude tcp)"
      echo
    } >> "${TCP_LOG}"
    sleep 5
  done
) &
WATCHER_PID=$!

cleanup() {
  echo
  echo "=== Cleaning up watcher (pid ${WATCHER_PID}) ==="
  kill "${WATCHER_PID}" 2>/dev/null || true
  wait "${WATCHER_PID}" 2>/dev/null || true

  echo "=== Generating summary ==="
  {
    echo "=== rc-debug summary  $(date -Iseconds) ==="
    echo
    echo "## 文件"
    ls -la "${DEBUG_LOG}" "${TCP_LOG}" 2>/dev/null
    echo
    echo "## 断开 / close / 重连关键事件（debug log，最后 50 行）"
    grep -inE 'close|disconnect|1006|1002|ECONNRESET|EPIPE|hang up|reconnect|timeout|websocket' \
        "${DEBUG_LOG}" 2>/dev/null | tail -50 || echo "  (none)"
    echo
    echo "## debug log 最后 40 行（看死前发生了什么）"
    tail -40 "${DEBUG_LOG}" 2>/dev/null
    echo
    echo "## TCP 连接数时间序列（每 5s 一行，最后 60 个采样点 = 5min）"
    awk '/^\[/{if(ts)print ts" active="cnt; ts=substr($0,2,8); cnt=0; next}
         /claude/{cnt++}
         END{if(ts)print ts" active="cnt}' "${TCP_LOG}" | tail -60
  } > "${SUMMARY_LOG}"

  echo
  echo "Summary 已写入：${SUMMARY_LOG}"
  echo
  echo "归类口诀："
  echo "  - close code 1006 → 1002 反复  →  服务端 25min bug (GitHub #31853)，自动重连"
  echo "  - ECONNRESET / EPIPE / socket hang up  →  代理或网络 RST（Clash idle timeout 嫌疑大）"
  echo "  - 无 close 事件但 TCP 连接突然消失  →  进程被杀 / NAT 老化 / 网络断"
  echo "  - 看到 401 / 403 / token  →  鉴权过期"
}
trap cleanup EXIT INT TERM

if [[ -n "${SESSION_NAME}" ]]; then
  claude remote-control --debug-file "${DEBUG_LOG}" --verbose --name "${SESSION_NAME}"
else
  claude remote-control --debug-file "${DEBUG_LOG}" --verbose
fi
