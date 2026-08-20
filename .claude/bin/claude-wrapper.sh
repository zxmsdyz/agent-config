#!/usr/bin/env bash
# claude wrapper: 默认只带 --dangerously-skip-permissions（不再默认写 debug-file）
#
# bash / zsh 双兼容：不用 bash-only 的 `type -aP` 或 process substitution，
# 改为纯 POSIX 循环手动遍历 $PATH。
#
# 安装：在 ~/.bashrc（或 ~/.zshrc）末尾加一行
#     source ~/.claude/bin/claude-wrapper.sh
#
# 跑原版（不加任何 wrap 参数）：
#     CLAUDE_RAW=1 claude --version
#
# 临时开独立 debug-file（排 RC 断连时用）：
#     CLAUDE_DEBUG=1 claude
#
# 故意要跑 Windows 版（基本没有正当理由）：
#     CLAUDE_ALLOW_WINDOWS=1 claude

# 解析出一个 **WSL 原生** 的 claude 可执行文件路径。
#
# Why（2026-08-14 事故）：PATH 里同时有
#     /home/kalami/.nvm/versions/node/*/bin/claude      ← WSL 版
#     /mnt/c/Users/*/AppData/Roaming/npm/claude         ← Windows 版 claude.exe 的 shim
# 某些 tmux pane 里 nvm 的 PATH 没生效，`claude` 就落到 Windows 那份。它会用 Git Bash 跑
# 项目 hook，而 Git Bash 的 `python3` 是微软商店占位 stub ⇒ qflow 的 13 个 PreToolUse guard
# 全部报 "Failed with non-blocking status code" 然后**放行**（fail-open，prod 写入无人拦）。
# 所以这里宁可拒绝启动，也不让 Windows 版接管 WSL 仓库。
# 遍历 PATH 找可执行的 claude。$1 = native 时跳过 /mnt/*（Windows claude.exe）。
#
# ⚠️ 不用 `for dir in $PATH`：zsh 默认不对未加引号的参数展开做分词（no SH_WORD_SPLIT），
#    那样整个 $PATH 会被当成一个词，查找会静默失效、直接掉进下面的 nvm 兜底。
#    这里用纯参数展开手工切分，bash / zsh / POSIX sh 行为一致。
_claude_scan_path() {
    local mode="$1"
    local rest="$PATH"
    local dir c
    while [ -n "$rest" ]; do
        dir="${rest%%:*}"
        case "$rest" in
            *:*) rest="${rest#*:}" ;;
            *)   rest="" ;;
        esac
        [ -n "$dir" ] || continue
        if [ "$mode" = native ]; then
            case "$dir" in
                /mnt/*) continue ;;
            esac
        fi
        c="$dir/claude"
        if [ -x "$c" ] && [ ! -d "$c" ]; then
            printf '%s\n' "$c"
            return 0
        fi
    done
    return 1
}

# 版本号降序排序。macOS 的 BSD sort 没有 -V，探测后退回字典序 -r。
# 探测用空输入，不消耗真实 stdin。
_claude_sort_desc() {
    if ! command -v sort >/dev/null 2>&1; then
        cat
    elif printf '' | sort -V >/dev/null 2>&1; then
        sort -V -r
    else
        sort -r
    fi
}

_claude_native_bin() {
    # 1) PATH 里第一个不在 /mnt/ 下的
    _claude_scan_path native && return 0

    # 2) PATH 没有 → 兜底扫 nvm（取版本号最大的一份）
    # ⚠️ 同样不能 `for c in $(...)`（zsh 不分词）。用 heredoc 而非管道喂 while，
    #    管道会把循环放进子 shell，里面的 return 0 退不出本函数。
    local c
    while IFS= read -r c; do
        [ -n "$c" ] || continue
        [ -x "$c" ] && { printf '%s\n' "$c"; return 0; }
    done <<EOF
$(ls -d "$HOME"/.nvm/versions/node/*/bin/claude 2>/dev/null | _claude_sort_desc)
EOF

    return 1
}

# 在 PATH 里找第一个可执行 claude（不排除 /mnt/*），供 CLAUDE_ALLOW_WINDOWS=1 使用。
_claude_any_bin() {
    _claude_scan_path any
}

claude() {
    local bin
    if [ "${CLAUDE_ALLOW_WINDOWS:-0}" = "1" ]; then
        bin=$(_claude_any_bin)
    elif ! bin=$(_claude_native_bin); then
        cat >&2 <<'EOF'
[claude-wrapper] ⛔ 拒绝启动：PATH 里找不到 WSL 原生的 claude，只有 /mnt/c 下的 Windows 版。
                 Windows 版 claude.exe 会用 Git Bash 跑 hook，那里的 python3 是微软商店占位 stub，
                 qflow 的 PreToolUse guard 会全部 fail-open（报错但放行 prod 写入 / main 提交）。
                 修法：source ~/.bashrc 让 nvm 的 PATH 生效，或直接跑 ~/.nvm/versions/node/*/bin/claude。
                 真要跑 Windows 版：CLAUDE_ALLOW_WINDOWS=1 claude
EOF
        return 127
    fi

    # Escape hatch 1: 完全跑原版（仍走 WSL 原生二进制，只是不加 wrap 参数）
    if [ "${CLAUDE_RAW:-0}" = "1" ]; then
        "$bin" "$@"
        return $?
    fi

    local extra_args
    extra_args="--dangerously-skip-permissions"

    # Opt-in: 只有显式 CLAUDE_DEBUG=1 才写 debug-file
    if [ "${CLAUDE_DEBUG:-0}" = "1" ]; then
        local logdir="$HOME/.claude/logs/rc-debug"
        mkdir -p "$logdir"
        # 每个 session 独立 log：时间戳 (秒) + 父 shell PID 保证唯一
        local logfile
        logfile="$logdir/interactive-$(date +%Y%m%d-%H%M%S)-pid$$.log"
        # 把 log 路径告诉用户，方便后续诊断时引用
        echo "[claude-wrapper] debug log → $logfile" >&2
        "$bin" $extra_args --debug-file "$logfile" "$@"
        return $?
    fi

    "$bin" $extra_args "$@"
}
