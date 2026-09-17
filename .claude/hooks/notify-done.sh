#!/usr/bin/env bash
# Claude Code Stop hook: 系统通知 + 语音朗读当前任务名(tmux 窗口名 #W)。
# 由 ~/.claude/settings.json 的 Stop hook 调用；弹窗+播放逻辑在同目录 notify.ps1。
#
# 语音：优先用 edge-tts 微软自然神经音(晓晓)合成 mp3 → macOS 用 afplay、WSL 交
#       notify.ps1 用 MediaPlayer 播放。WSL/国内网络下 edge-tts 直连 wss 被墙，需经
#       http 代理；macOS 本机实测可直连(~1s)，故无代理时直接跑、不再跳过。合成失败/
#       超时时退回本地合成音兜底(macOS=Tingting，WSL=WinRT Yaoyao)，永不哑火。
# - 在 tmux 里：标题 = "[<session>] Claude Code"，正文/朗读 = 当前窗口名(#W)=任务摘要
# - 不在 tmux 里：退回固定文案 "任务已完成"
#
# 两类值得打扰用户的时刻，由 hook_event_name 分发：
#   Stop                          → "任务已完成"
#   Notification/PermissionRequest→ "需要你确认"（等权限 / 空闲提醒）
#   PreToolUse                    → "需要你确认"（AskUserQuestion / ExitPlanMode，靠
#                                   settings 的 matcher 限定；本机 bypassPermissions
#                                   下权限弹窗几乎不出现，卡住等人的就是这两个工具）
# 只响一次：Stop 只注册在主 agent（subagent 走 SubagentStop，不注册），进门再过一遍
# claude-notify-root-turn.py，把 subagent 内的 Stop、`--agent` 会话，以及"起完后台
# 活就先结束本回合"的中途停顿全部静音。⚠ 等人确认那条**不能**套后台活判据——起了后台
# 活又撞上要授权是常态，套上去就等于"最该响的时候不响"，故过滤器分 mode。
#
# 自测：CLAUDE_NOTIFY_DRY_RUN=1 只打印 kind/title/line/speech 四行，不弹窗不发声。

# 本文件可能是指向仓库的软链——解析真实路径，定位同目录的过滤器 / notify.ps1 / venv
self_path="$0"
while [ -L "$self_path" ]; do
  link=$(readlink "$self_path")
  case "$link" in
    /*) self_path="$link" ;;
    *) self_path="$(dirname "$self_path")/$link" ;;
  esac
done
selfdir=$(cd "$(dirname "$self_path")" && pwd -P)

# 只在「主 agent 回合真正结束」时通知：subagent / --agent 会话 / 还在等后台活的
# 回合一律静默。判据全在 claude-notify-root-turn.py 的注释里。
payload=$(cat 2>/dev/null)

read_field() {
  python3 -c 'import json,sys
try:
    p = json.loads(sys.argv[1])
except Exception:
    p = {}
print((p.get(sys.argv[2]) or "") if isinstance(p, dict) else "")' "$payload" "$1" 2>/dev/null
}

event=$(read_field hook_event_name)
case "$event" in
  # payload 取不到时按 Stop 走：只注册了 Stop 的事件不可能是别的，静默只会白吃通知
  # （与过滤器的 fail-open 同源）。
  ""|Stop)
    filter_mode="stop"
    status_text="任务已完成"
    ;;
  Notification|PermissionRequest|PreToolUse)
    filter_mode="attention"
    # Notification 没有 message 字段，只有 notification_type；PreToolUse 只有 tool_name。
    # 播报词由它们决定，否则横幅只剩一句没信息量的"需要你确认"。
    status_text="需要你确认"
    case "$(read_field notification_type)" in
      permission_prompt) status_text="需要你授权" ;;
      elicitation_dialog|elicitation_url_dialog) status_text="需要你填写" ;;
      agent_needs_input) status_text="后台任务需要你输入" ;;
    esac
    case "$(read_field tool_name)" in
      AskUserQuestion) status_text="有问题要你选" ;;
      ExitPlanMode) status_text="计划待你批准" ;;
    esac
    ;;
  *)
    exit 0
    ;;
esac

if ! printf '%s' "$payload" | python3 "$selfdir/claude-notify-root-turn.py" --mode "$filter_mode"; then
  exit 0
fi

# 想换嗓音改这里：zh-CN-XiaoxiaoNeural(晓晓/女·温柔) / zh-CN-YunxiNeural(云希/男) /
#                zh-CN-XiaoyiNeural(晓伊/女) / zh-CN-YunyangNeural(云扬/男·播音)
VOICE="zh-CN-XiaoxiaoNeural"

title="Claude Code $status_text"
line="$status_text"

if [ -n "$TMUX_PANE" ] && command -v tmux >/dev/null 2>&1; then
  sess=$(tmux display-message -p -t "$TMUX_PANE" '#S' 2>/dev/null)
  task=$(tmux display-message -p -t "$TMUX_PANE" '#W' 2>/dev/null)
  [ -n "$sess" ] && title="[$sess] Claude Code $status_text"
  [ -n "$task" ] && line="$task"
fi

# 朗读内容在覆盖 line 之前定下来：万一某版本真给了 message，那是英文
# （"Claude needs your permission to use Bash"），交给中文神经音念出来含混且拖长。
# ⇒ 横幅优先用 message（具体、看得见），朗读只用 tmux 任务名 + 中文状态（短、听得清）。
if [ "$line" = "$status_text" ]; then
  speech="$status_text"
else
  speech="${line}，${status_text}"
fi

# 官方文档没展开 Notification 的完整字段，message 未必存在——有就用，没有就保持窗口名。
if [ "$filter_mode" = "attention" ]; then
  message=$(read_field message)
  [ -n "$message" ] && line="$message"
fi

if [ "${CLAUDE_NOTIFY_DRY_RUN:-}" = "1" ]; then
  printf '%s\n%s\n%s\n%s\n' "$filter_mode" "$title" "$line" "$speech"
  exit 0
fi

macos_say() {
  text=$1
  # ⚠ 探测 Tingting 是否存在只能用 `say -v '?'`（列出所有声音，纯 stdout 不发声）。
  # 曾误写成 `say -v Tingting '?'` —— 那是「用婷婷朗读文本 ?」，于是每次任务完成
  # 都会先听到一声「问号」再听任务名。重定向挡得住文字挡不住声音。
  if say -v '?' 2>/dev/null | grep -q '^Tingting[[:space:]]'; then
    say -v Tingting "$text" >/dev/null 2>&1
  else
    say "$text" >/dev/null 2>&1
  fi
}

repo_root=$(cd "$selfdir/../.." && pwd -P)
edgetts="$repo_root/.venv/bin/edge-tts"

# edge-tts 合成 mp3。all_proxy(socks5) 会干扰 wss，显式 -u 排除；只走 http 代理。
# timeout 8s 防代理挂时 hook 卡死；失败/超时留空文件，交由 ps1 兜底。
mp3="$selfdir/_tts.mp3"
rm -f "$mp3"
proxy="${https_proxy:-${http_proxy:-}}"
run_with_timeout() {
  if command -v timeout >/dev/null 2>&1; then timeout 8 "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout 8 "$@"
  else "$@"
  fi
}
if [ -x "$edgetts" ]; then
  if [ -n "$proxy" ]; then
    run_with_timeout env -u all_proxy "$edgetts" --proxy "$proxy" --voice "$VOICE" \
      --text "$speech" --write-media "$mp3" >/dev/null 2>&1
  else
    # 无代理(macOS 本机)：直连 wss。曾因 `[ -n "$proxy" ]` 一律跳过，导致 mac 上
    # 即使装了 edge-tts 也永远听不到晓晓，只有本地兜底音。
    run_with_timeout env -u all_proxy "$edgetts" --voice "$VOICE" \
      --text "$speech" --write-media "$mp3" >/dev/null 2>&1
  fi
fi

case "$(uname -s)" in
  Darwin)
    # 参数通过 argv 传入 AppleScript，避免标题中的引号被当作脚本内容解析。
    osascript - "$title" "$line" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
APPLESCRIPT
    if [ -s "$mp3" ] && command -v afplay >/dev/null 2>&1; then
      afplay "$mp3" >/dev/null 2>&1
    elif command -v say >/dev/null 2>&1; then
      macos_say "$speech"
    fi
    ;;
  Linux)
    if command -v wslpath >/dev/null 2>&1 && command -v powershell.exe >/dev/null 2>&1; then
      audio_win=""
      [ -s "$mp3" ] && audio_win=$(wslpath -w "$mp3" 2>/dev/null)
      ps1win=$(wslpath -w "$selfdir/notify.ps1" 2>/dev/null)
      powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps1win" \
        -Title "$title" -Line "$line" -AudioPath "$audio_win" >/dev/null 2>&1
    else
      command -v notify-send >/dev/null 2>&1 && notify-send "$title" "$line" >/dev/null 2>&1
      [ -s "$mp3" ] && command -v mpv >/dev/null 2>&1 && mpv --no-video "$mp3" >/dev/null 2>&1
    fi
    ;;
esac

exit 0
