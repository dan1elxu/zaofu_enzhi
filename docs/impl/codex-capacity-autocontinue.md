# Codex 容量报错自动继续

[监控脚本](../../scripts/codex_capacity_autocontinue.py)用于 Linux 的交互式 Codex CLI。
依赖 Python 3.11+、tmux、ps，不需要安装 Python 包。

在仓库根目录运行：

```bash
# 先观察一轮，不发送按键
python3 scripts/codex_capacity_autocontinue.py --dry-run --once

# 持续监控；Ctrl-C 停止
python3 scripts/codex_capacity_autocontinue.py

# 只监控一个 session，连续报错至少间隔 60 秒重试
python3 scripts/codex_capacity_autocontinue.py --session zf-test_dev5 --cooldown 60
```

默认每 2 秒扫描所选 tmux server 的所有窗格，动态发现新窗格。
在 tmux 内启动时使用当前 server；在外部启动时使用当前用户的默认 server。
自定义 server 使用 `--socket /path/to/tmux.sock`。每个 server 只运行一个监控进程。

需要后台运行时，在本机终端执行（脚本路径按实际位置调整）：

```bash
tmux new-session -d -s codex-capacity-watch \
  python3 /home/uisee/zaofu/scripts/codex_capacity_autocontinue.py

# 查看监控输出
tmux attach -t codex-capacity-watch
```

在监控窗格中按 Ctrl-C 停止；也可用
`tmux kill-session -t '=codex-capacity-watch'` 关闭这个独立监控 session。

普通 GNOME Terminal、VS Code Terminal、SSH 终端必须先在 tmux 中启动 Codex，
才能由该脚本读取屏幕并发送输入，例如 `tmux new-session -s codex-work codex`。
脚本不会接管已经在 tmux 外运行的进程，也不适用于 Codex App、IDE 聊天面板或
`codex exec` / app-server 会话。

## 触发条件

- 使用 tty 的前台进程组和当前用户 UID 确认 Codex；支持原生可执行程序和 Node wrapper。
- 当前可见屏幕最后一段输出必须是完整的
  `⚠ Selected model is at capacity. Please try a different model.`，随后为 Codex 空输入框。
- 忽略警告与输入框之间仅含单点粒子动画的装饰行，以及完整匹配
  `N background terminal running · /ps to view · /stop to close` 的后台终端状态栏；
  动画和状态栏刷新不会触发重复重试，真实文字、忙碌提示和输入草稿仍会阻止发送。
- 非空草稿、正在运行、复制模式、退出到 shell、其他报错和历史中的旧警告不触发。
- 发送文字 `继续`，等待 0.5 秒，再单独发送 Enter；两次发送前检查 pane 身份和前台进程。
- 同一条警告只尝试一次。再次出现新警告时，按 pane 独立冷却，默认至少间隔 30 秒。
  旧警告仍在屏幕上时，新警告也能识别。部分发送失败会报错且不重复追加文字。

这是屏幕自动化，依赖 Codex 的 `›` 输入提示、灰色 placeholder 和警告格式。
无法确认输入状态时不发送。窗格过小导致警告不可见、CLI 更换布局，或滚屏缓冲区
饱和后两轮画面完全相同时，可能漏检；可放大窗格并用 dry-run 检查。
去重仅保存在监控进程内存，重启后当前警告可重新触发。
更新脚本后，需要在监控窗口按 Ctrl-C 并重新运行原命令，已有进程不会自动加载修改。
自动发送“继续”只代替手动重试，服务仍然满载时不保证恢复。

## 验证

```bash
.venv/bin/python -m pytest tests/test_codex_capacity_autocontinue.py -q --no-cov

# 显式 host 层：独立 socket + 模拟 Codex，验证真实 tmux 中文输入与清理
.venv/bin/python -m pytest tests/test_codex_capacity_autocontinue.py -q --no-cov -m host
```

脚本通过 tmux 发送输入，不读写 ZaoFu 的 ledger、canonical stores 或配置。
没有改动 ZaoFu Kernel 的派发、恢复或完成状态判定。
