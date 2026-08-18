#!/usr/bin/env bash
# Codex notify：完成一轮任务后弹 Windows toast，并朗读当前 tmux 任务名。

VOICE="zh-CN-XiaoxiaoNeural"
payload="${1:-{}}"
title="Codex"
line="任务已完成"

if [ -n "${TMUX_PANE:-}" ] && command -v tmux >/dev/null 2>&1; then
  sess=$(tmux display-message -p -t "$TMUX_PANE" '#S' 2>/dev/null)
  task=$(tmux display-message -p -t "$TMUX_PANE" '#W' 2>/dev/null)
  [ -n "$sess" ] && title="[$sess] Codex"
  [ -n "$task" ] && line="$task"
fi

if [ "$line" = "任务已完成" ]; then
  summary=$(python3 -c 'import json,sys; p=json.loads(sys.argv[1]); xs=p.get("input-messages", []); print(" ".join(xs).strip()[:80])' "$payload" 2>/dev/null)
  [ -n "$summary" ] && line="$summary"
fi

selfdir=$(dirname "$(readlink -f "$0")")
notify_ps1="/home/kalami/claude-config/.claude/hooks/notify.ps1"
edgetts="/home/kalami/claude-config/.venv/bin/edge-tts"
mp3="$selfdir/_tts.mp3"
rm -f "$mp3"

proxy="${https_proxy:-${http_proxy:-}}"
if [ -x "$edgetts" ] && [ -n "$proxy" ]; then
  timeout 8 env -u all_proxy "$edgetts" --proxy "$proxy" --voice "$VOICE" \
    --text "$line" --write-media "$mp3" >/dev/null 2>&1
fi

audio_win=""
[ -s "$mp3" ] && audio_win=$(wslpath -w "$mp3" 2>/dev/null)
ps1win=$(wslpath -w "$notify_ps1" 2>/dev/null)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps1win" \
  -Title "$title" -Line "$line" -AudioPath "$audio_win" >/dev/null 2>&1

exit 0
