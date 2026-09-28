#!/usr/bin/env bash
# Codex 终态提醒：按会话 ID 朗读原生短名，兼容 macOS、WSL 与 Linux。

if [ "$#" -gt 0 ]; then
  payload="$1"
  input_mode="legacy"
else
  input_mode="hook"
  payload=$(cat)
fi

self_path="$0"
while [ -L "$self_path" ]; do
  link=$(readlink "$self_path")
  case "$link" in /*) self_path="$link" ;; *) self_path="$(dirname "$self_path")/$link" ;; esac
done
selfdir=$(cd "$(dirname "$self_path")" && pwd -P)

event=$(python3 -c 'import json,sys; p=json.loads(sys.argv[1]); print(p.get("hook_event_name") or p.get("type") or "")' "$payload" 2>/dev/null)
case "$event" in
  Stop)
    notification_kind="complete"
    status_text="任务已完成"
    ;;
  PermissionRequest)
    notification_kind="decision"
    status_text="需要你做决策"
    ;;
  agent-turn-complete)
    # 旧版 notify 会同时通知根线程和 subagent，只允许真实根线程播报。
    if ! python3 "$selfdir/codex-notify-root-turn.py" "$payload"; then
      exit 0
    fi
    last_message=$(python3 -c 'import json,sys; p=json.loads(sys.argv[1]); print((p.get("last-assistant-message") or "").strip())' "$payload" 2>/dev/null)
    [ -n "$last_message" ] || exit 0
    notification_kind="complete"
    status_text="任务已完成"
    ;;
  *)
    [ "$input_mode" = "hook" ] && printf '{}\n'
    exit 0
    ;;
esac

VOICE="zh-CN-XiaoxiaoNeural"
title="Codex $status_text"
line="$status_text"
task=$(printf '%s' "$payload" | python3 "$selfdir/codex-thread-name.py" 2>/dev/null)
[ -n "$task" ] && line="$task"

if [ "$line" = "$status_text" ]; then
  speech="$status_text"
else
  speech="${line}，${status_text}"
fi

macos_say() {
  text=$1
  # ⚠ 探测声音只能用 `say -v '?'`（列声音表，不发声）；写成 `say -v Tingting '?'`
  # 就是「用婷婷朗读文本 ?」，每次通知都会先念一声「问号」。同 notify-done.sh。
  if say -v '?' 2>/dev/null | grep -q '^Tingting[[:space:]]'; then
    say -v Tingting "$text" >/dev/null 2>&1
  else
    say "$text" >/dev/null 2>&1
  fi
}

if [ "${CODEX_NOTIFY_DRY_RUN:-}" = "1" ]; then
  printf '%s\n%s\n%s\n%s\n' "$notification_kind" "$title" "$line" "$speech"
  [ "$input_mode" = "hook" ] && printf '{}\n'
  exit 0
fi

repo_root=$(cd "$selfdir/../.." && pwd -P)
notify_ps1="$repo_root/.claude/hooks/notify.ps1"
edge_python="${CODEX_NOTIFY_EDGE_PYTHON:-$repo_root/.venv/bin/python3}"
tts_dir=$(mktemp -d "${TMPDIR:-/tmp}/codex-notify.XXXXXX" 2>/dev/null) || tts_dir=""
mp3=""
if [ -n "$tts_dir" ]; then
  mp3="$tts_dir/voice.mp3"
  trap 'rm -f "$mp3"; rmdir "$tts_dir"' EXIT
fi

proxy="${https_proxy:-${http_proxy:-}}"
run_with_timeout() {
  if command -v timeout >/dev/null 2>&1; then timeout 8 "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout 8 "$@"
  else "$@"
  fi
}
if [ -n "$mp3" ] && [ -x "$edge_python" ]; then
  if [ -n "$proxy" ]; then
    run_with_timeout env -u all_proxy "$edge_python" -m edge_tts --proxy "$proxy" --voice "$VOICE" \
      --text "$speech" --write-media "$mp3" >/dev/null 2>&1
  else
    # 无代理(macOS 本机)：直连 wss 实测可用，不再整段跳过。同 notify-done.sh。
    run_with_timeout env -u all_proxy "$edge_python" -m edge_tts --voice "$VOICE" \
      --text "$speech" --write-media "$mp3" >/dev/null 2>&1
  fi
fi

case "$(uname -s)" in
  Darwin)
    osascript - "$title" "$line" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
APPLESCRIPT
    if [ -s "$mp3" ] && command -v afplay >/dev/null 2>&1; then afplay "$mp3" >/dev/null 2>&1
    elif command -v say >/dev/null 2>&1; then macos_say "$speech"
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

[ "$input_mode" = "hook" ] && printf '{}\n'
exit 0
