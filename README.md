# agent-config

个人 Claude Code、Codex CLI + tmux 配置备份，支持 WSL、macOS 和原生 Linux。

## 文件映射

仓库内路径镜像 `$HOME`，安装时拷回（或 symlink）到对应位置：

| 仓库内 | 部署到 | 作用 |
|---|---|---|
| `.tmux.conf` | `~/.tmux.conf` | tmux 配置；窗口名跟随 `pane_title`，只剥离明确的 spinner，保留正常标题首字；pane 边框右端显示 Claude Code peer 地址 |
| `.claude/bin/tmux-cc-peer-label.sh` | `~/.claude/bin/tmux-cc-peer-label.sh` | 被 `pane-border-format` 的 `#()` 调用，查 `~/.claude/sessions/<pid>.json` 反查该 pane 的 Claude Code peer 地址：`⇄ 名字 sid` = 已注册 peer messaging（`SendMessage({to:"名字"})` 可直达）；`· 名字 sid (no-msg)` = 有会话但没开 messaging socket，`ListAgents` 看不到它 |
| `.claude/settings.json` | `~/.claude/settings.json` | Claude Code 设置；`Stop`（任务完成）、`Notification`（等权限 / MCP 表单）、`PreToolUse`（matcher 限定 `AskUserQuestion｜ExitPlanMode`）三个 hook 都指向下面的脚本 |
| `.claude/hooks/notify-done.sh` | `~/.claude/hooks/notify-done.sh` | 通知入口：自动判断 macOS / WSL / Linux，选择对应的通知和语音实现。按 `hook_event_name` 分发成「任务已完成」与「需要你确认」两类；subagent 完成与「等后台活」的中途停顿保持静默。`CLAUDE_NOTIFY_DRY_RUN=1` 只打印 mode/标题/正文/播报词四行，不弹窗不发声 |
| `.claude/hooks/claude-notify-root-turn.py` | 随 `notify-done.sh` 同目录 | payload 过滤器，分 `stop` / `attention` 两种 mode。共同拦掉 subagent（`agent_id`/`agent_type`、`subagents/` 下的 transcript）；`stop` 额外拦 `background_tasks` 还有在飞任务的回合，`attention` 额外拦 `idle_prompt` 等噪声通知类型与非阻塞工具。每次静音记一行到 `~/.claude/notify-skip.log` |
| `.claude/hooks/notify.ps1` | 随 `notify-done.sh` 同目录 | 弹 Windows toast + 用 WinRT OneCore 嗓音 **Yaoyao（女声）** 朗读任务名；找不到该嗓音则退回默认 |
| `.codex/hooks.json` | `~/.codex/hooks.json` | Codex `UserPromptSubmit` / `Stop` / `PermissionRequest` hook；原生名称优先，缺名时请求语义标题 |
| `.codex/bin/codex-pane-title.py` | `~/.codex/bin/codex-pane-title.py` | 按 hook 的 `session_id` 核验 TUI 能力文件与根会话，只读查询原生名称；为空时注入语义命名指令，由原生 `set_thread_title` 工具设置当前会话标题；不读取继承的 `TMUX_PANE` |
| `.codex/bin/codex-thread-name.py` | `~/.codex/bin/codex-thread-name.py` | 用 hook 的 `session_id` 精确只读查询本机 `state_5.sqlite` 的 `threads.name`；无短名就退回状态词，不借其他 pane 标题 |
| `.codex/bin/codex-notify-done.sh` | `~/.codex/bin/codex-notify-done.sh` | Codex `Stop` / `PermissionRequest` hook 与旧版 `notify` 入口；语音读对应会话短名和状态，保留桌面通知，旧版 subagent 完成保持静默 |
| `.codex/rules/default.rules` | `~/.codex/rules/default.rules` | Codex 用户级规则；允许 `git merge` 免二次确认 |
| `.gemini/bin/agy-pane-title.py` | `~/.gemini/bin/agy-pane-title.py` | Antigravity (`agy`) 终端标题与语义命名助手（仅在检测到本机原生 `agy` 时安装）：无参模式供 `settings.json.title` (`TitleRunner`) 调用输出当前会话标题（由 BubbleTea OSC 2 刷入 `pane_title`）；`--hook` 模式供 `PreInvocation` 校验 TUI 根会话（`presence/<id>.lock` + 非 subagent）并在缺少中文标题或切换任务时注入语义命名指令；`--set` 模式写入会话标题并同步更新当前 tmux pane 标题 |
| `.gemini/antigravity-cli/settings.json` | 与 `~/.gemini/antigravity-cli/settings.json` 合并 | Antigravity CLI 终端标题配置（`title.command` 指向 `agy-pane-title.py`），仅在检测到本机原生 `agy` 时合并，保留本机 `colorScheme` / `trustedWorkspaces` 等其余字段 |
| `.gemini/config/hooks.json` | 与 `~/.gemini/config/hooks.json` 合并 | Antigravity `PreInvocation` hook（`agy-pane-title`），仅在检测到本机原生 `agy` 时合并，保留本机其他自定义 hook |
| `.claude/bin/claude-wrapper.sh` | `~/.claude/bin/claude-wrapper.sh` | 定义 `claude()` shell 函数，默认加 `--dangerously-skip-permissions` 并拒绝 `/mnt/*` 下的 Windows 版 claude 接管 WSL 仓库；bash / zsh 双兼容（纯 POSIX 循环遍历 `$PATH`，不用 `type -aP` / process substitution） |
| `.claude/bin/rc-debug.sh` | `~/.claude/bin/rc-debug.sh` | remote-control 断连排障：带 `--debug-file` 启动 + 后台采样 TCP 连接状态 |
| `.claude/bin/rc-disconnect-snapshot.sh` | `~/.claude/bin/rc-disconnect-snapshot.sh` | 手机端报 disconnected 时一键采证（bridge 进程活性 / poll 时间 / environment_id 等） |
| `.claude/bin/statusline-agent-name.sh` | `~/.claude/bin/statusline-agent-name.sh` | Claude Code `statusLine`：显示本 session 的跨 agent 通信名（查 `~/.claude/sessions/<pid>.json`）+ 当前 git 分支 + model |
| `.claude/settings.local.json` | `~/.claude/settings.local.json` | 个人 `permissions.allow` 白名单（仅命令前缀级授权，无凭据）。⚠️ 该文件名默认被本机全局 `~/.config/git/ignore` 忽略，本仓库 `.gitignore` 用 `!.claude/settings.local.json` 显式取消忽略 |
| `.claude/skills/cryptostruct-market-data/` | `~/.claude/skills/cryptostruct-market-data/` | 第三方行情数据技能（整目录 symlink），无凭据，仅文档 + 拉取脚本 |
| `shell/rc.snippet` | 注入 `~/.bashrc` / `~/.zshrc` | `cxyolo` / `ccyolo` / `agyolo` alias + `claude-wrapper.sh` 的 source 行；用 `# >>> agent-config >>>` / `# <<< agent-config <<<` marker 包裹，幂等注入（重复执行 `install.sh` 不会重复追加） |
| `.codex/config.toml` | 与 `~/.codex/config.toml` 合并 | 见下方「codex config.toml 合并策略」 |

## 安装（软链接方式）

```bash
cd ~/agent-config
./install.sh
```

`install.sh` 会在最开头完成**全部探测和校验**（python3 版本、openspec-mcp），任何一项
不满足就直接退出，不会改动这台机器上的任何文件。校验通过后才开始安装。

tmux 部分是自动的：脚本发现 `.tmux.conf` 里引用了 tpm 就会自动 `git clone` tpm 并同步
`@plugin` 声明的插件（resurrect / continuum）。已有 tmux server 在跑时会顺带
`source-file` 一次，所以不需要再手动执行 `tmux source-file ~/.tmux.conf`。

`install.sh` 会识别 macOS / WSL / Linux，大部分配置以软链接安装。若目标位置已有普通文件，
脚本会先备份再覆盖——备份**统一落到 `~/.agent-config-backups/`**（文件名是相对 `$HOME`
的路径展平 + 时间戳），不留在原地。原地留备份会出事：`~/.claude/skills/` 和
`~/.codex/rules/` 是**按目录扫描**的，一个 `<name>.bak.<时间戳>` 会被当成另一个 skill /
另一份规则加载出来。以下两个例外**不是** symlink，而是
每次执行 `install.sh` 时按本机情况**生成 / 合并**出来的本地文件：

- `~/.claude/settings.json`：由通用模板加上本机自动探测到的 MCP 路径生成。
- `~/.codex/config.toml`：见下方「codex config.toml 合并策略」。

`~/.bashrc` / `~/.zshrc` 里的 `cxyolo` / `ccyolo` alias + `claude-wrapper.sh` source 行通过
marker 包裹区间幂等注入（见上表 `shell/rc.snippet`），不是 symlink（rc 文件里其余
个人内容不能被覆盖，只能注入/替换 marker 内的这一段）。macOS 默认目标是
`~/.zshrc`，Linux/WSL 默认目标是 `~/.bashrc`；若两个 rc 文件在本机都存在，会同时注入两份，
保证换 shell 也生效。

### codex config.toml 合并策略

`~/.codex/config.toml` 除了 `model` / `model_reasoning_effort` / `notify` / `[tui].terminal_title` /
`[mcp_servers.openspec]` 这几个跨机器通用字段外，还含机器专属的
`[projects."<本机路径>"]` trust_level、`[hooks.state...]` 信任哈希、
`[tui.model_availability_nux]`、`[notice.model_migrations]`，不能直接整体覆盖或
symlink 模板。`install.sh` 的处理方式：

脚本不只看 PATH 里的 `python3`——它按 `python3.14` → `python3.13` → `python3.12` →
`python3.11` → `python3` 的顺序探测，取第一个带 `tomllib` 的。macOS 自带的 `python3` 是
3.9.6，但只要机器上另装了 3.11+，合并就照常生效，**不需要**为此调整 PATH。

1. 若探测到带 `tomllib` 的 python3（3.11+）：解析本机已有的 `config.toml`（若存在），
   只覆盖 `model` / `model_reasoning_effort` / `[tui].terminal_title` / `[mcp_servers.openspec]`；仅移除指向旧 `codex-notify-done.sh` 的 `notify`，保留其他桌面通知入口。
   其中 `mcp_servers.openspec.command` 会替换成实际探测到的可执行文件；其余段原样保留后写回；写回前会用
   `tomllib.loads()` 自检一遍，解析成功后才落盘。原文件会先备份成
   `config.toml.bak.<timestamp>`。`[tui]` 里的其他设置原样保留。
2. 若本机没有该文件：直接按模板生成。
3. 若整台机器都找不到 3.11+ 的 python3：**不做合并**——已有文件保持原样不动，
   只在终端提示需要手动核对 `model` / `model_reasoning_effort` / `[tui].terminal_title` /
   `[mcp_servers.openspec]`，并手动移除指向旧 `codex-notify-done.sh` 的 `notify`；没有旧文件时才按模板直接生成（不做
   TOML 解析校验）。装一个 3.11+ 的 python3 即可恢复合并，无需其他配置。

> 已知限制：合并是「解析 → 重新序列化」，**`~/.codex/config.toml` 里的注释会在合并后丢失**
> （TOML 注释不进解析结果）。本机手写的注释请放在别处，或接受每次 `install.sh` 后消失。

> 注：`.claude/settings.json` / `.codex/config.toml` 仓库里放的是**通用模板**，不是某台
> 机器的直接副本，改本机那两个文件不会回流仓库——要让改动跨机器生效必须改模板。
> **其余文件装完都是 symlink，仓库即唯一真源**：编辑 `~/.claude/bin/xxx.sh` 就是在编辑
> 仓库里的那份，`git status` 会直接看到，不需要"同步回仓库"这一步。

## 依赖

- macOS 通知使用系统自带的 `osascript`，语音优先播放 edge-tts 产物，否则回退到 `say`。
- ⚠️ **Claude Code 在会话启动时快照 hooks 配置**：改完 `settings.json` 要新开会话（或在
  会话里跑 `/hooks` 复核）才生效，当前会话不会自动加载。
- 通知没响时先看 `~/.claude/notify-skip.log`——静音本身不可观测，判据误伤的现象就是
  「通知莫名其妙没了」，这份日志记下是哪条判据拦的、`background_tasks` 当时什么状态。
- 刻意**不**注册 Claude 侧的 `PermissionRequest`：`Notification` 的 `permission_prompt`
  已覆盖同一时刻，且它是决策卡点，hook 跑得慢会拖住授权对话框本身。
- edge-tts（微软自然神经音·晓晓）由 `install.sh` 自动建在仓库内 `.venv/`（已 gitignore，幂等、装不上只警告不阻断）。WSL/国内网络需 `https_proxy`/`http_proxy` 才能连 wss；macOS 本机实测可直连，无代理也会走神经音。缺失时退回本地合成音（macOS=Tingting，WSL=WinRT Yaoyao），不会哑火。
- WSL 的 Windows toast 需 PowerShell 模块 `BurntToast`（`Install-Module BurntToast`），原有逻辑保持不变。
- 原生 Linux 通知优先使用 `notify-send`，MP3 播放使用 `mpv`（存在时）。
- Codex TUI 使用原生 `thread-name` 终端标题；原生名称为空时，`UserPromptSubmit` hook 引导 Codex 概括任务并调用原生标题工具。能力文件和会话 ID 将此行为限定在 TUI 根会话，`exec` / `review` / subagent 不改名；`/clear` 后新会话独立命名。通知脚本靠 hook 会话 ID 读取同一个短名，不依赖继承的 `TMUX_PANE`。
- Codex 首次发现或脚本变更后会要求审核 hook；在 Codex 内运行 `/hooks` 并信任该用户级 hook。
- `git`：用于自动安装 tpm。缺失时脚本只警告，`.tmux.conf` 里的插件不会生效。
- `tmux`：未安装时脚本会跳过插件同步并提示；装好 tmux 后重跑 `install.sh` 即可补上。
- `python3` 3.11+（任意版本号形式均可，脚本自动探测）：`~/.codex/config.toml` 的安全合并所需。

`install.sh` 会把 `.claude/settings.json` 模板里 `enabledPlugins` 标为 `true` 的插件
**逐个装上并启用**（目前是 `superpowers` / `frontend-design` /
`skill-creator`）。只在 `settings.json` 里写 `enable` 而不 install，新机器上会留下
「已启用但不存在」的插件——所以插件清单以模板为准，加插件只需改模板。同时自动探测
`openspec-mcp` 可执行文件或包含 `openspec_mcp` 模块的 Python 虚拟环境。未安装且
系统存在 `uv` 时，会自动安装 `openspec-mcp` 并固定兼容的 `mcp<2`，然后通过
`claude mcp add --scope user` 注册，不依赖 Claude 不读取的 `settings.json.mcpServers` 路径。
非标准安装位置可在执行时显式传入：

```bash
OPENSPEC_MCP_PYTHON=/path/to/venv/bin/python \
./install.sh
```

## 不包含

不含任何凭证 / token / cookie / 会话数据（如 `~/.claude/.credentials.json`、`~/.claude/projects/` 等），仅纯配置。
