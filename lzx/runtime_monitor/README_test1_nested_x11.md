# test1 可重复 runtime monitor 实验

宿主桌面是 GNOME Wayland，不能可靠地用 `xdotool` 激活原生窗口。test1 实验使用 Xephyr 创建隔离 X11 显示，并由 Openbox 管理窗口；应用、自动化和 monitor 只使用这个显示，不需要注销当前桌面。

运行：

```bash
cd /home/lzx/Desktop/huawei_mem/lzx
MONITOR_DURATION=240 \
  runtime_monitor/scripts/run_test1_nested_x11_experiment.sh
```

脚本会：

- 启动 Firefox 官方 Linux 二进制、LibreOffice、VLC、GIMP、Audacity、Thunderbird、Telegram、Evince、PCManFM（映射为 `FILES`）和 Calculator；
- 记录两轮逐窗口切换、启动、最小化、恢复和关闭；
- 使用 250ms 采样、`close_grace_windows=1` 生成 `process_events.csv`、`foreground_events.csv` 和 `app_lifecycle_events.csv`；
- 用 `verify_test1_event_coverage.py` 对自动化动作和 monitor 事件逐项核对，任何缺失事件都会使脚本返回非零状态。

验收文件：

`outputs/runtime_monitor/<session>/review/event_coverage.json`

其中 `status=PASS` 且 `missing_actions` 为空，才算有效实验。
