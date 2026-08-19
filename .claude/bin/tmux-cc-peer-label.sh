#!/usr/bin/env bash
# 在 tmux pane 边框上显示该 pane 里 Claude Code 会话的 peer 地址。
#
# 用法（由 tmux pane-border-format 的 #() 调用）：
#   tmux-cc-peer-label.sh '#{session_name}' '#{window_id}' '#{pane_id}'
#
# 真源 = ~/.claude/sessions/<pid>.json 的 tmux / name / messagingSocketPath 字段。
#   ⇄ name      → 已开 messaging socket，SendMessage({to:"name"}) 可直达
#   · name      → 有会话但没注册 peer messaging，ListAgents 看不到它
#   (空)        → 这个 pane 里没跑 Claude Code
#
# sid 是 sessionId 前 8 位（磁盘可查）。注意 ListAgents 打印的 [xxxxxx] 短 ref 是
# 运行期生成、不落盘，无法在这里复现——名字本身就是地址，短 ref 只在重名时才需要。
set -u

key="${1:-}:${2:-}.${3:-}"
dir="$HOME/.claude/sessions"
[ -d "$dir" ] || exit 0

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
[ -n "$best" ] || exit 0

name=$(sed -n 's/.*"name":"\([^"]*\)".*/\1/p' "$best")
[ -n "$name" ] || name="(unnamed)"
sid=$(sed -n 's/.*"sessionId":"\([0-9a-f]\{8\}\).*/\1/p' "$best")

if grep -qF '"messagingSocketPath"' "$best"; then
  printf '⇄ %s %s' "$name" "$sid"
else
  printf '· %s %s (no-msg)' "$name" "$sid"
fi
