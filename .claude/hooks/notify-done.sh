#!/usr/bin/env bash
# Claude Code Stop hook: Windows toast + 语音朗读当前任务名(tmux 窗口名 #W)。
# 由 ~/.claude/settings.json 的 Stop hook 调用；弹窗+播放逻辑在同目录 notify.ps1。
#
# 语音：优先用 edge-tts 微软自然神经音(晓晓)合成 mp3 → notify.ps1 用 MediaPlayer 播放；
#       edge-tts 需经 http 代理(直连 wss 被墙)。代理挂/合成失败时，notify.ps1 退回本地
#       WinRT Yaoyao 老合成音兜底，永不哑火。
# - 在 tmux 里：标题 = "[<session>] Claude Code"，正文/朗读 = 当前窗口名(#W)=任务摘要
# - 不在 tmux 里：退回固定文案 "任务已完成"

# 想换嗓音改这里：zh-CN-XiaoxiaoNeural(晓晓/女·温柔) / zh-CN-YunxiNeural(云希/男) /
#                zh-CN-XiaoyiNeural(晓伊/女) / zh-CN-YunyangNeural(云扬/男·播音)
VOICE="zh-CN-XiaoxiaoNeural"

title="Claude Code"
line="任务已完成"

if [ -n "$TMUX_PANE" ] && command -v tmux >/dev/null 2>&1; then
  sess=$(tmux display-message -p -t "$TMUX_PANE" '#S' 2>/dev/null)
  task=$(tmux display-message -p -t "$TMUX_PANE" '#W' 2>/dev/null)
  [ -n "$sess" ] && title="[$sess] Claude Code"
  [ -n "$task" ] && line="$task"
fi

# 本文件可能是指向仓库的软链——解析真实路径，定位同目录的 notify.ps1 / venv
selfdir=$(dirname "$(readlink -f "$0")")
edgetts="/home/kalami/claude-config/.venv/bin/edge-tts"

# edge-tts 合成 mp3。all_proxy(socks5) 会干扰 wss，显式 -u 排除；只走 http 代理。
# timeout 8s 防代理挂时 hook 卡死；失败/超时留空文件，交由 ps1 兜底。
mp3="$selfdir/_tts.mp3"
rm -f "$mp3"
proxy="${https_proxy:-${http_proxy:-}}"
if [ -x "$edgetts" ] && [ -n "$proxy" ]; then
  timeout 8 env -u all_proxy "$edgetts" --proxy "$proxy" --voice "$VOICE" \
    --text "$line" --write-media "$mp3" >/dev/null 2>&1
fi

audio_win=""
[ -s "$mp3" ] && audio_win=$(wslpath -w "$mp3" 2>/dev/null)

ps1win=$(wslpath -w "$selfdir/notify.ps1" 2>/dev/null)

# 参数直传给 PowerShell（不进 PS 字符串字面量，无需转义单引号）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps1win" \
  -Title "$title" -Line "$line" -AudioPath "$audio_win" >/dev/null 2>&1

exit 0
