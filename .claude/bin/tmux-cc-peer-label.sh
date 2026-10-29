#!/usr/bin/env bash
# 在 tmux pane 边框上显示该 pane 里 Claude Code 或 Antigravity (agy) 会话的 peer 地址。
#
# 用法（由 tmux pane-border-format 的 #() 调用）：
#   tmux-cc-peer-label.sh '#{session_name}' '#{window_id}' '#{pane_id}'
#
# 1. Claude Code 真源 = ~/.claude/sessions/<pid>.json 的 tmux / name / messagingSocketPath 字段：
#      ⇄ name sid          → 已开 messaging socket，SendMessage({to:"name"}) 可直达
#      · name sid (no-msg) → 有会话但没注册 peer messaging，ListAgents 看不到它
# 2. Antigravity (agy) 真源 = ~/.gemini/antigravity-cli/titles/pane-<pane_id>.json 与 presence/<cid>.lock：
#      ⇄ 标题或窗口名 sid   → 8 位短 ID（send_message / --peers 可直达）
# 3. (空)                  → 这个 pane 里没跑 Claude Code 或 agy
set -u

pane_id="${3:-}"
key="${1:-}:${2:-}.${pane_id}"
dir="$HOME/.claude/sessions"

if [ -d "$dir" ]; then
  best=""
  best_t=0
  shopt -s nullglob
  for f in "$dir"/*.json; do
    grep -qF "\"tmux\":\"$key\"" "$f" || continue
    pid="${f##*/}"; pid="${pid%.json}"
    kill -0 "$pid" 2>/dev/null || continue   # Linux / macOS 都可用的存活检查
    t=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0)
    if [ "$t" -ge "$best_t" ]; then best_t=$t; best="$f"; fi
  done
  if [ -n "$best" ]; then
    name=$(sed -n 's/.*"name":"\([^"]*\)".*/\1/p' "$best")
    [ -n "$name" ] || name="(unnamed)"
    sid=$(sed -n 's/.*"sessionId":"\([0-9a-f]\{8\}\).*/\1/p' "$best")

    if grep -qF '"messagingSocketPath"' "$best"; then
      printf '⇄ %s %s' "$name" "$sid"
    else
      printf '· %s %s (no-msg)' "$name" "$sid"
    fi
    exit 0
  fi
fi

# Antigravity (agy) 分支
[ -n "$pane_id" ] || exit 0
agy_home="${AGY_HOME:-$HOME/.gemini/antigravity-cli}"
pane_map="$agy_home/titles/pane-${pane_id}.json"
agy_script="$HOME/.gemini/bin/agy-pane-title.py"

if [ -f "$pane_map" ]; then
  agy_pid=$(sed -n 's/.*"pid":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$pane_map" | head -1)
  cid=$(sed -n 's/.*"conversation_id":[[:space:]]*"\([0-9a-f-]*\)".*/\1/p' "$pane_map" | head -1)
  sid=$(sed -n 's/.*"short_id":[[:space:]]*"\([0-9a-f]\{8\}\)".*/\1/p' "$pane_map" | head -1)
  if [ -n "$agy_pid" ] && [ -n "$cid" ] && [ -n "$sid" ] && kill -0 "$agy_pid" 2>/dev/null && [ -f "$agy_home/presence/${cid}.lock" ]; then
    title_file="$agy_home/titles/${cid}.txt"
    ann_file="$agy_home/annotations/${cid}.pbtxt"
    # 若用户在 agy 内通过 /rename 更新了 annotations，走一次 python 同步
    if [ -f "$ann_file" ] && [ -f "$title_file" ] && [ "$ann_file" -nt "$title_file" ] && [ -f "$agy_script" ]; then
      python3 "$agy_script" --pane-label "$pane_id" 2>/dev/null && exit 0
    fi
    title=""
    [ -f "$title_file" ] && title=$(head -n 1 "$title_file" 2>/dev/null | tr -d '\r\n')
    [ -n "$title" ] || title=$(sed -n 's/.*"title":[[:space:]]*"\([^"]*\)".*/\1/p' "$pane_map" | head -1)

    # 若用户在 tmux 手动 rename-window（automatic-rename 为 0），优先显示手动窗口名
    tmux_info=$(tmux display-message -p -t "$pane_id" '#{automatic-rename}	#{window_name}' 2>/dev/null || true)
    auto_flag="${tmux_info%%	*}"
    win_name="${tmux_info#*	}"
    if { [ "$auto_flag" = "0" ] || [ "$auto_flag" = "off" ]; } && [ -n "$win_name" ]; then
      title="$win_name"
    fi
    [ -n "$title" ] || title="agy"
    printf '⇄ %s %s' "$title" "$sid"
    exit 0
  else
    rm -f "$pane_map" 2>/dev/null || true
  fi
fi

# 首次发现尚未写入 pane-<id>.json 的在跑 agy 面板（闲置 shell 面板无子进程直接跳过，不启 python）
if [ -f "$agy_script" ]; then
  pane_info=$(tmux display-message -p -t "$pane_id" '#{pane_current_command}	#{pane_pid}' 2>/dev/null || true)
  pane_cmd="${pane_info%%	*}"
  pane_pid="${pane_info#*	}"
  if [ "$pane_cmd" = "agy" ] || { [ -n "$pane_pid" ] && pgrep -P "$pane_pid" >/dev/null 2>&1; }; then
    python3 "$agy_script" --pane-label "$pane_id" 2>/dev/null || true
  fi
fi
