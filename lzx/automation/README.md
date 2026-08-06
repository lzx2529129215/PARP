# Linux 应用自动化

本目录用于 Linux 桌面应用自动化：打开应用、执行点击/按键/输入/拖拽、关闭应用、切换应用。

WPS 0010–0070 七个性能场景、样本准备和结果验证工具已合并到本目录，详见
[`README_WPS.md`](README_WPS.md)。统一入口为：

```bash
cd /home/lzxxxxxx/桌面/huawei/huawei_mem/lzx
automation/run_wps_case.sh 0070 --dry-run
```

## QQ、Files、WPS 多场景套件

新增的丰富场景通过统一入口执行：

```bash
cd /home/lzxxxxxx/桌面/huawei/huawei_mem/lzx
automation/run_rich_scenario.sh --list
automation/run_rich_scenario.sh qq --dry-run
automation/run_rich_scenario.sh files --dry-run
automation/run_rich_scenario.sh wps --dry-run
automation/run_rich_scenario.sh cross --dry-run
```

场景覆盖：

- `qq`：启动与登录状态验证、联系人搜索、会话打开、历史消息滚动、草稿输入和清理、最小化/恢复。
- `files`：在 `outputs/automation_rich/files_workspace` 隔离目录中执行多层导航、搜索、列表/网格视图切换、新建目录和返回父目录。
- `wps`：Writer 文档输入、搜索和另存，Spreadsheet 单元格定位、表格输入和另存，Presentation 新增幻灯片、放映和另存。
- `cross`：Files 查找任务素材，WPS 生成报告，Files 检索产物，QQ 生成通知草稿后清除。

QQ 场景默认搜索“我的电脑”，可以指定测试会话：

```bash
automation/run_rich_scenario.sh qq \
  --var QQ_TEST_CONTACT="测试会话" \
  --var QQ_DRAFT_TEXT="automation safe draft"
```

QQ 和跨应用场景只输入草稿，随后使用 `Ctrl+A` + `BackSpace` 清除，不执行发送。如果搜索结果或快捷键与当前 QQ 版本不同，应先使用 `--dry-run` 和 `--calibration-only` 校准，不要直接添加发送键。

### Thunderbird、VLC、GIMP、KeePassXC、LibreOffice

五个桌面应用已加入同一个场景入口：

```bash
automation/run_rich_scenario.sh thunderbird --dry-run
automation/run_rich_scenario.sh vlc --dry-run
automation/run_rich_scenario.sh gimp --dry-run
automation/run_rich_scenario.sh keepassxc --dry-run
automation/run_rich_scenario.sh libreoffice --dry-run
automation/run_rich_scenario.sh five-app --dry-run
```

- Thunderbird：邮件窗口启动、全局搜索、邮件夹/邮件列表浏览、地址簿和日历切换；不发送邮件。
- VLC：本地视频打开、播放/暂停、进度跳转、音量、静音、全屏和播放列表。
- GIMP：仅编辑 `outputs/automation_rich/gimp/working_copy.png` 隔离副本，覆盖缩放、画布浏览、工具栏/全屏切换、图像复制和 XCF 保存。
- KeePassXC：只运行启动页、打开/新建数据库对话框的取消流程、密码生成器、设置和窗口状态，不打开真实密码库。
- LibreOffice：Writer 创建/搜索/保存 ODT，Calc 生成场景矩阵 ODS，Impress 新建幻灯片、放映和保存 ODP。
- `five-app`：一次启动五应用，循环两轮执行切换、前台验证和应用内代表操作，可用于模拟桌面多任务。

上述场景的样本和可写产物都限定在仓库 `samples/` 与 `outputs/automation_rich/` 中。正式运行前建议先 dry-run，然后在当前分辨率下做一次真实 UI 校准。

## 依赖

基础启动/关闭只需要 Python。窗口聚焦、点击、按键需要 `xdotool`：

```bash
sudo apt install xdotool
```

## 运行

一键运行自动化：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
automation/run_automation.sh
```

当前推荐实验场景使用 Files/Nautilus 作为第三个应用：

```bash
automation/run_automation.sh --scenario configs/automation/scenario_local_files.json
```

带 action trace 输出：

```bash
automation/run_automation.sh \
  --scenario configs/automation/scenario_local_files.json \
  --trace-output outputs/runtime_monitor/session_files_001/model/automation_trace.csv \
  --session-id session_files_001 \
  --scenario-id scenario_local_files
```

dry-run 检查：

```bash
automation/run_automation.sh --dry-run
```

手动运行底层 Python：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
python3 automation/app_automation.py configs/automation/scenario_local_wps.json --dry-run
python3 automation/app_automation.py configs/automation/scenario_local_wps.json
```

如果出现 `Can't open display: (null)`，说明当前终端没有 `DISPLAY` 环境变量。优先在桌面终端运行；也可以显式指定 X11 display：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
python3 automation/app_automation.py configs/automation/scenario_local_wps.json --display :0
```

如果是通过 `sudo` 或其他用户执行，可能还需要指定 Xauthority：

```bash
python3 automation/app_automation.py configs/automation/scenario_local_wps.json --display :0 --xauthority /home/lzx/.Xauthority
```

GNOME Wayland + Xwayland 下，Xauthority 通常不在 `~/.Xauthority`，而在 `/run/user/1000/.mutter-Xwaylandauth.*`。脚本会自动探测。这个参数主要给 `xdotool`、WPS、QQ 这类 X11/Xwayland 控制使用：

```bash
python3 automation/app_automation.py configs/automation/scenario_local_wps.json \
  --display :0 \
  --xauthority /run/user/1000/.mutter-Xwaylandauth.OI83Q3
```

如果当前桌面是 Wayland 原生会话，`xdotool` 只能控制 Xwayland 窗口，可能无法控制原生 Wayland 窗口。

## 场景动作

场景文件是 JSON，核心字段是 `actions`：

```json
{
  "actions": [
    {"type": "launch", "name": "wps", "command": "wps"},
    {"type": "wait", "seconds": 5},
    {"type": "key", "key": "ctrl+o"},
    {"type": "wait", "seconds": 2},
    {"type": "key", "key": "Escape"},
    {"type": "close", "name": "wps"}
  ]
}
```

支持的动作：

- `launch`：启动程序，字段：`name`、`command`
- `wait`：等待，字段：`seconds`
- `focus`：聚焦窗口，字段：`title` 或 `class`
- `switch`：优先聚焦窗口；找不到且提供 `command` 时启动程序
- `key` / `hotkey`：发送快捷键，例如 `ctrl+o`、`alt+Tab`、`Escape`
- `type` / `text`：输入文本，字段：`text`、可选 `delay_ms`
- `click` / `tap`：点击屏幕坐标，字段：`x`、`y`、可选 `button`
- `drag` / `swipe`：拖拽，字段：`x1`、`y1`、`x2`、`y2`、可选 `duration_ms`
- `close`：关闭窗口或进程，字段：`title`、`class`、`name` 或 `command`
- `close` 也支持 `process_names`、`path_contains`、`cmdline_contains`、`wait_after_window_close`、`force_after_seconds`，适合 WPS/Firefox 这类启动命令和实际进程不同的应用
- `shell`：执行本机 shell 命令，适合串联采集脚本
- `optional: true`：动作失败时只打印 warning 并继续，当前 Firefox 相关动作默认是 optional

WPS 如果没有被关闭，通常是因为 `wps` 启动命令只是 wrapper，真实进程可能是 `wpsoffice` 或 `/opt/kingsoft/wps-office/...`。不要使用 `pkill -f et` 这类宽泛匹配，它可能误杀 SSH/VS Code Server 等命令行中包含相同短字符串的进程。示例场景使用精确进程名和安装路径做兜底：

```json
{
  "type": "close",
  "name": "wps",
  "class": "wps",
  "process_names": ["wpsoffice", "wps", "wpp", "et", "wpspdf"],
  "path_contains": ["/opt/kingsoft/wps-office"],
  "wait_after_window_close": 2,
  "force_after_seconds": 3
}
```

当前 `configs/automation/scenario_local_wps.json` 还包含 QQ 流程：

- 启动 WPS。
- 启动 QQ。
- 启动 Firefox。
- 聚焦 WPS。
- 从 WPS 切换到 QQ。
- 从 QQ 切换到 Firefox。
- 从 QQ 切回 WPS。
- 再切换到 QQ。
- 再切换到 Firefox。
- 最后关闭 Firefox 窗口、QQ 和 WPS。

场景文件选择：

- `configs/automation/scenario_local_wps.json`：原 Firefox 场景。当前 Snap Firefox + Wayland 环境下可能无法被 `xdotool` 控制。
- `configs/automation/scenario_local_files.json`：推荐当前实验使用的稳定场景，用 Files/Nautilus 替代 Firefox。

QQ 启动命令默认尝试：

```bash
gtk-launch qq || gtk-launch linuxqq || linuxqq || qq
```

如果你的 QQ 启动方式不同，先查启动项或命令：

```bash
find /usr/share/applications ~/.local/share/applications -iname '*qq*.desktop' -print
command -v linuxqq
command -v qq
```

然后修改 `configs/automation/scenario_local_wps.json` 里启动 QQ 的 `shell` action，以及 QQ `switch/close` action 里的 `class/title/process_names/path_contains`。

Files/Nautilus 启动命令：

```bash
nautilus --new-window /home/lzx
```

如果系统没有 `nautilus`：

```bash
sudo apt install nautilus
```

文件管理器窗口可控性测试：

```bash
GDK_BACKEND=x11 nautilus --new-window /home/lzx
xdotool search --onlyvisible --class Nautilus
xdotool search --onlyvisible --class org.gnome.Nautilus
xdotool search --onlyvisible --name Home
```

如果这些命令能输出窗口 ID，说明 `xdotool` 可以控制文件管理器。

Nautilus 的 `net usershare`、`Gdk`、`GLib` warning 当前可以忽略，不影响窗口切换和关闭验证。

Firefox 通过场景中的 `type: shell` action 启动。当前自动化使用 `xdotool` 搜索、激活和关闭窗口，因此 Firefox 必须以 Xwayland/X11 窗口方式启动：

```bash
mkdir -p /home/lzx/firefox_profiles/automation && env -u WAYLAND_DISPLAY MOZ_ENABLE_WAYLAND=0 GDK_BACKEND=x11 firefox --new-instance --no-remote -profile /home/lzx/firefox_profiles/automation --new-window about:blank
```

Wayland 原生 Firefox 无法被 `xdotool search` 发现，也无法用 `xdotool windowactivate` 激活。后续如果要支持 Wayland 原生窗口，需要接 GNOME Shell Extension 或 compositor 级接口；当前阶段不接这些接口。

当前机器上的 Firefox 是 **snap** 版。Snap Firefox 有两个关键限制：

1. **私有 /tmp 挂载命名空间**：主机上创建的 `/tmp/...` 在 Snap Firefox 进程内不可见，会导致 "Profile Missing" 错误。因此自动化专用 profile **必须放在 `/home/lzx/firefox_profiles/automation`**（snap 的 `home` 接口允许访问 `/home/lzx/`，但不能访问主机的 `/tmp/`）。

2. **需要完整图形环境变量**：从 VS Code / TTY 终端启动时，`DISPLAY`、`WAYLAND_DISPLAY`、`XAUTHORITY` 等变量为空。`run_automation.sh` 会自动检测并补齐这些变量，然后 `app_automation.py` 的 `_cgroup_launch_shell()` 通过 `systemd-run --user --scope --setenv=...` 注入给 Snap Firefox 进程。

命令中不再需要 `nohup`、末尾 `&`、外层 `sh -c`、手动 `export` 环境变量——这些由 `run_automation.sh`（环境检测）和 `app_automation.py`（systemd-run 注入）统一处理。

`mkdir -p`（而非 `rm -rf && mkdir -p`）避免在上次 Firefox 未完全退出时破坏正在使用的 profile 数据库。

关闭时按命令行里的 profile 路径（`/home/lzx/firefox_profiles/automation`）匹配 PID，只关闭自动化启动的 Firefox，不会用 `pkill -x firefox` 误关你已有的浏览器会话。

Firefox 的切换动作仍保留 `optional: true`：如果窗口管理器或启动环境异常导致 `xdotool` 找不到 Firefox，会跳过并继续 WPS/QQ 流程。
如果 Firefox 没有打开，先看 systemd journal 日志（自动化通过 systemd-run --scope 启动）：

```bash
journalctl --user -u automation-firefox.scope --no-pager -n 50
```

### 单独测试 Firefox 启动

如果自动化中 Firefox 启动失败，可以单独运行以下命令验证 Snap Firefox + systemd-run 是否正常：

```bash
systemd-run --user --scope --unit=automation-firefox-test \
  --setenv=DISPLAY=:0 \
  --setenv=XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.OI83Q3 \
  --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
  --setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  --setenv=MOZ_ENABLE_WAYLAND=0 \
  --setenv=GDK_BACKEND=x11 \
  --setenv=HOME=/home/lzx \
  --setenv=USER=lzx \
  --setenv=LOGNAME=lzx \
  --setenv=PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin" \
  firefox --new-instance --no-remote -profile /home/lzx/firefox_profiles/automation --new-window about:blank
```

如果这个命令成功，但 `./run_automation.sh` 中 Firefox 启动失败，说明环境变量未完整传递到 systemd-run。检查 `run_automation.sh` 的诊断输出中 `DISPLAY`、`XAUTHORITY`、`DBUS_SESSION_BUS_ADDRESS`、`GDK_BACKEND` 是否正确。

### 验证 Firefox 是否可被 xdotool 控制

Firefox 启动后执行：

```bash
xdotool search --onlyvisible --class firefox
xdotool search --onlyvisible --name Firefox
xprop -id $(xdotool search --onlyvisible --class firefox | tail -1) WM_CLASS _NET_WM_NAME
```

如果能输出 Firefox window id 和 `WM_CLASS`，说明 Firefox 当前是 Xwayland/X11 窗口，`xdotool` 可以控制它。

## 和 runtime_monitor 配合

一个终端先启动采集：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
python3 runtime_monitor/monitor.py --output-dir outputs/runtime_monitor \
  --session-id wps_auto --label WPS_OPEN_DOC --path-mode hash
```

另一个终端运行自动化：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
python3 automation/app_automation.py configs/automation/scenario_local_wps.json
```

如果你已经在另一个终端启动了 monitor，可以用一键脚本只跑自动化：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
automation/run_automation.sh
```

## 推荐完整流程：采集、自动化、对齐

终端 A：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx

python3 runtime_monitor/monitor.py \
  --config configs/runtime/config.yaml \
  --target-app WPS \
  --sample-interval 1 \
  --output-dir outputs/runtime_monitor \
  --session-id session_files_001 \
  --path-mode hash
```

终端 B：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx

automation/run_automation.sh \
  --scenario configs/automation/scenario_local_files.json \
  --trace-output outputs/runtime_monitor/session_files_001/model/automation_trace.csv \
  --session-id session_files_001 \
  --scenario-id scenario_local_files
```

对齐：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx

python3 runtime_monitor/scripts/align_automation_monitor.py \
  --features outputs/runtime_monitor/session_files_001/features_1s.csv \
  --trace outputs/runtime_monitor/session_files_001/model/automation_trace.csv \
  --output outputs/runtime_monitor/session_files_001/features_1s.labeled.csv \
  --labels-output outputs/runtime_monitor/session_files_001/labels.csv \
  --state-label-mode carry-forward
```

输出：

- `automation_trace.csv`：自动化每个 action 的 start/end trace。
- `labels.csv`：由 trace 配对得到的 action label 区间。
- `features_1s.labeled.csv`：将 action label 对齐到 runtime monitor 特征后的训练数据。
- `state_label`：启用 `--state-label-mode carry-forward` 时生成；WAIT 秒会继承最近一次非 WAIT 动作状态。

`features_1s.csv` 现在包含 `session_id`、`feature_window_id`、`window_start_ns`、`window_end_ns`、`window_title`，用于和 `automation_trace.csv` 做时间窗口对齐。`foreground_app` 由 X11/Xwayland active window 采集，不再使用 `--target-app` 固定填充。

当前流程不会执行任何预取、page cache 驱逐、swap、MGLRU 或内存调度动作。
