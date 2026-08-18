# claude-config

个人 Claude Code、Codex CLI + tmux 配置备份。

## 文件映射

仓库内路径镜像 `$HOME`，安装时拷回（或 symlink）到对应位置：

| 仓库内 | 部署到 | 作用 |
|---|---|---|
| `.tmux.conf` | `~/.tmux.conf` | tmux 配置；开启 `automatic-rename`，窗口名自动跟随 Claude Code 当前任务摘要（取 `pane_title` 剥掉 spinner），新 tab 不再是 `bash`、无需手动改名；pane 边框右端显示该 pane 的 CC peer 地址（见下一行脚本） |
| `.claude/bin/tmux-cc-peer-label.sh` | `~/.claude/bin/tmux-cc-peer-label.sh` | 被 `pane-border-format` 的 `#()` 调用，查 `~/.claude/sessions/<pid>.json` 反查该 pane 的 Claude Code peer 地址：`⇄ 名字 sid` = 已注册 peer messaging（`SendMessage({to:"名字"})` 可直达）；`· 名字 sid (no-msg)` = 有会话但没开 messaging socket，`ListAgents` 看不到它 |
| `.claude/settings.json` | `~/.claude/settings.json` | Claude Code 设置；`Stop` hook 调用下面的脚本弹 Windows 通知 |
| `.claude/hooks/notify-done.sh` | `~/.claude/hooks/notify-done.sh` | Stop hook 入口：算好 tmux 标题/任务名，调同目录 `notify.ps1` |
| `.claude/hooks/notify.ps1` | 随 `notify-done.sh` 同目录 | 弹 Windows toast + 用 WinRT OneCore 嗓音 **Yaoyao（女声）** 朗读任务名；找不到该嗓音则退回默认 |
| `.codex/hooks.json` | `~/.codex/hooks.json` | Codex `UserPromptSubmit` hook；按首条任务内容自动设置 tmux 窗口名，后续短追问不会覆盖 |
| `.codex/bin/codex-pane-title.py` | `~/.codex/bin/codex-pane-title.py` | 读取 Codex hook JSON，把首条 prompt 压成简短 pane/window 名 |
| `.codex/bin/codex-notify-done.sh` | `~/.codex/bin/codex-notify-done.sh` | Codex `notify` 入口；复用 Windows toast + 晓晓语音，在每轮完成时朗读 tmux 任务名 |

## 安装（拷贝方式）

```bash
cp .tmux.conf ~/.tmux.conf
mkdir -p ~/.claude/hooks
cp .claude/settings.json ~/.claude/settings.json
cp .claude/hooks/notify-done.sh ~/.claude/hooks/notify-done.sh
chmod +x ~/.claude/hooks/notify-done.sh
mkdir -p ~/.codex/bin
ln -s ~/claude-config/.codex/hooks.json ~/.codex/hooks.json
ln -s ~/claude-config/.codex/bin/codex-pane-title.py ~/.codex/bin/codex-pane-title.py
ln -s ~/claude-config/.codex/bin/codex-notify-done.sh ~/.codex/bin/codex-notify-done.sh
chmod +x ~/claude-config/.codex/bin/*
tmux source-file ~/.tmux.conf   # 让运行中的 tmux 立即生效
```

> 注：仓库里是**副本**，之后改了本机 `~` 下的文件记得同步回仓库再 commit。
> 想让仓库成为唯一真源、编辑自动同步，可改用 symlink（如 GNU stow）。

## 依赖

- Windows toast 通知需 PowerShell 模块 `BurntToast`（`Install-Module BurntToast`）。
- `notify-done.sh` 通过 WSL interop 调 `powershell.exe`，仅在 WSL 环境有效。
- `~/.codex/config.toml` 需配置 `notify = ["bash", "/home/kalami/.codex/bin/codex-notify-done.sh"]`。
- Codex 首次发现或脚本变更后会要求审核 hook；在 Codex 内运行 `/hooks` 并信任该用户级 hook。

## 不包含

不含任何凭证 / token / cookie / 会话数据（如 `~/.claude/.credentials.json`、`~/.claude/projects/` 等），仅纯配置。
