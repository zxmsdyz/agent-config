#!/usr/bin/env bash
# Codex notify：完成一轮任务后弹系统通知，并朗读当前 tmux 任务名。

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

self_path="$0"
while [ -L "$self_path" ]; do
  link=$(readlink "$self_path")
  case "$link" in /*) self_path="$link" ;; *) self_path="$(dirname "$self_path")/$link" ;; esac
done
selfdir=$(cd "$(dirname "$self_path")" && pwd -P)
repo_root=$(cd "$selfdir/../.." && pwd -P)
notify_ps1="$repo_root/.claude/hooks/notify.ps1"
edgetts="$repo_root/.venv/bin/edge-tts"
mp3="$selfdir/_tts.mp3"
rm -f "$mp3"

proxy="${https_proxy:-${http_proxy:-}}"
run_with_timeout() {
  if command -v timeout >/dev/null 2>&1; then timeout 8 "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout 8 "$@"
  else "$@"
  fi
}
if [ -x "$edgetts" ] && [ -n "$proxy" ]; then
  run_with_timeout env -u all_proxy "$edgetts" --proxy "$proxy" --voice "$VOICE" \
    --text "$line" --write-media "$mp3" >/dev/null 2>&1
fi

case "$(uname -s)" in
  Darwin)
    osascript - "$title" "$line" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
APPLESCRIPT
    if [ -s "$mp3" ] && command -v afplay >/dev/null 2>&1; then afplay "$mp3" >/dev/null 2>&1
    elif command -v say >/dev/null 2>&1; then say "$line" >/dev/null 2>&1
    fi
    ;;
  Linux)
    if command -v wslpath >/dev/null 2>&1 && command -v powershell.exe >/dev/null 2>&1; then
      audio_win=""
      [ -s "$mp3" ] && audio_win=$(wslpath -w "$mp3" 2>/dev/null)
      ps1win=$(wslpath -w "$notify_ps1" 2>/dev/null)
      powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps1win" \
        -Title "$title" -Line "$line" -AudioPath "$audio_win" >/dev/null 2>&1
    else
      command -v notify-send >/dev/null 2>&1 && notify-send "$title" "$line" >/dev/null 2>&1
      [ -s "$mp3" ] && command -v mpv >/dev/null 2>&1 && mpv --no-video "$mp3" >/dev/null 2>&1
    fi
    ;;
esac

exit 0
