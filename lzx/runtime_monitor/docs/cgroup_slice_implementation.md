# Runtime Monitor cgroup 设置实现说明

本文档说明当前项目如何把 automation 打开的 WPS、QQ、FILES 放入同一个实验 cgroup 父 slice，并让 Runtime Monitor 读取该 slice 的资源指标。当前实现只做进程归属、资源统计和验证，不包含预取、驱逐、swap、MGLRU、debugfs 或 page cache 调度动作。

## 目标

当前链路的目标是：

1. automation 启动应用时，把目标应用进程放入 `huawei-test.slice`。
2. 每个应用可以有独立的 systemd scope，例如 `automation-wps.scope`、`automation-qq.scope`、`automation-files.scope`。
3. Runtime Monitor 只采集实验 slice 内的应用进程，避免把桌面环境中已有的同名进程混入数据。
4. 在输出中记录 slice 级内存统计，例如 `memory.current`、`memory.high`、`memory.max`。
5. 提供独立检查脚本，验证 WPS / QQ / FILES 是否确实位于 `huawei-test.slice` 父 cgroup 下。

## 调用入口

典型启动命令：

```bash
SESSION_ID=session_online_lstm_$(date +%Y%m%d_%H%M%S)

./automation/run_automation.sh \
  --scenario configs/automation/scenario_local_wps_files_qq_auto_login.json \
  --session-id "$SESSION_ID" \
  --scenario-id scenario_local_wps_files_qq_auto_login \
  --trace-output "outputs/runtime_monitor/${SESSION_ID}/model/automation_trace.csv" \
  --test-slice huawei-test.slice \
  --reset-files
```

这里的关键参数是 `--test-slice huawei-test.slice`。该参数从 shell 入口继续传给 Python automation runner，最终作为 `systemd-run --slice` 的参数使用。

## run_automation.sh 的职责

文件：`automation/run_automation.sh`

该脚本负责做三类准备工作。

第一，补齐 GUI 应用需要的用户会话环境变量，包括：

- `DISPLAY`
- `XAUTHORITY`
- `XDG_RUNTIME_DIR`
- `DBUS_SESSION_BUS_ADDRESS`
- `WAYLAND_DISPLAY`
- `GDK_BACKEND`
- `MOZ_ENABLE_WAYLAND`
- `HOME`
- `USER`
- `PATH`

这些变量会被后续 `systemd-run --user --scope` 继承或显式注入。这样 WPS、QQ、FILES 等 GUI 应用即使由 systemd scope 启动，也能连接到当前桌面会话。

第二，设置实验 slice 的 accounting 开关：

```bash
systemctl --user set-property "${TEST_SLICE}" MemoryAccounting=yes CPUAccounting=yes IOAccounting=yes
```

这一步针对的是用户级 systemd slice，也就是 `systemctl --user` 管理的 `huawei-test.slice`。它不是系统级的 `systemctl status huawei-test.slice`。

第三，把 `--test-slice` 继续传递给 `automation/app_automation.py`：

```bash
ARGS+=("--test-slice" "$TEST_SLICE")
python3 "$SCRIPT_DIR/app_automation.py" "${ARGS[@]}"
```

## app_automation.py 如何创建 cgroup scope

文件：`automation/app_automation.py`

automation runner 中的核心逻辑是用 `systemd-run --user --scope` 启动应用。

对于普通 `launch` 动作，调用链是：

```text
launch()
  -> _cgroup_available()
  -> _cgroup_launch(command, name, env, test_slice=ctx.test_slice)
```

对于需要 shell 语法的 `shell` 动作，调用链是：

```text
_handle_shell()
  -> _cgroup_available()
  -> _cgroup_launch_shell(command, name, env, test_slice=ctx.test_slice)
```

`_cgroup_launch()` 和 `_cgroup_launch_shell()` 都会构造类似下面的命令：

```bash
systemd-run --user --scope \
  --unit=automation-wps \
  --slice=huawei-test.slice \
  --setenv=DISPLAY=:0 \
  --setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  ...
  wps
```

systemd 实际创建的 unit 会带 `.scope` 后缀，所以 WPS 的 leaf cgroup 通常是：

```text
/user.slice/user-1000.slice/user@1000.service/huawei.slice/huawei-test.slice/automation-wps.scope
```

QQ 和 FILES 也采用同样方式，分别进入：

```text
/user.slice/user-1000.slice/user@1000.service/huawei.slice/huawei-test.slice/automation-qq.scope
/user.slice/user-1000.slice/user@1000.service/huawei.slice/huawei-test.slice/automation-files.scope
```

因此，当前设计不是要求所有应用进程处于同一个 leaf cgroup，而是要求它们处于同一个父 slice：

```text
/user.slice/user-1000.slice/user@1000.service/huawei.slice/huawei-test.slice
```

这可以同时满足两点：

- 父 slice 上能读取统一的资源统计。
- 每个应用仍有独立 scope，方便停止、kill 和排查。

## unit 名称规则

automation 会根据场景中的 `name` 字段生成 unit 名称：

```text
WPS   -> automation-wps.scope
QQ    -> automation-qq.scope
FILES -> automation-files.scope
```

名称会经过清理，只保留字母、数字、点、下划线和横线，避免非法 systemd unit 字符。

## 环境变量注入

`app_automation.py` 中维护了 `_GUI_ENV_KEYS` 白名单，只把 GUI 应用确实需要的环境变量通过 `--setenv=KEY=VALUE` 注入 systemd scope。

这样做的原因是：`systemd-run --user --scope` 启动的进程不一定自动拥有当前 shell 的完整图形会话环境。显式注入后，应用才能稳定访问 X11/Xwayland、Wayland、DBus、Snap/桌面启动器路径等。

## Runtime Monitor 如何识别 test slice

Runtime Monitor 侧使用同一个 `--test-slice huawei-test.slice` 参数。

### 解析真实 cgroup 路径

文件：`runtime_monitor/core/feature_builder.py`

`FeatureBuilder` 会调用：

```bash
systemctl --user show huawei-test.slice -p ControlGroup
```

把 systemd unit 名称解析成真实 cgroup 路径，例如：

```text
ControlGroup=/user.slice/user-1000.slice/user@1000.service/huawei.slice/huawei-test.slice
```

最终记录到输出字段：

```text
test_slice=huawei-test.slice
test_slice_path=/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/huawei.slice/huawei-test.slice
```

### 只采集 slice 内进程

文件：`runtime_monitor/collectors/process.py`

进程采集器会读取每个 PID 的 `/proc/<pid>/cgroup`。当启用 `--test-slice` 时，只保留 cgroup 路径中包含目标 slice 的进程：

```text
.../huawei-test.slice/automation-wps.scope
.../huawei-test.slice/automation-qq.scope
.../huawei-test.slice/automation-files.scope
```

不在该 slice 下的同名进程会被跳过。这样可以减少桌面已有应用、后台服务或历史残留进程对数据集的污染。

### 读取 slice 级内存指标

文件：`runtime_monitor/monitor.py`

`monitor.py` 中的 `_read_test_slice_memory()` 同样先通过 `systemctl --user show` 解析真实 ControlGroup，然后读取：

```text
memory.current
memory.high
memory.max
```

这些值会写入 1 秒粒度特征，例如：

```text
test_mem_current
test_mem_high
test_mem_max
```

注意：这里是读取 cgroup v2 文件中的统计值，不做内存调度或内核策略修改。

## automation trace 中的 cgroup 记录

automation 每一步会写出 `automation_trace.csv`。其中包含：

- `pid`
- `tgid`
- `cgroup_path`
- `window_id`
- `window_title`
- `app`
- `action`
- `label`

当应用窗口被识别到时，trace 中的 `cgroup_path` 可以直接看到类似：

```text
0::/user.slice/user-1000.slice/user@1000.service/huawei.slice/huawei-test.slice/automation-wps.scope
```

这用于把“应用操作步骤”和“应用进程 cgroup 归属”关联起来。

## cgroup 验证脚本

文件：`runtime_monitor/scripts/check_huawei_cgroup_membership.sh`

该脚本用于独立验证 automation 启动的 WPS / QQ / FILES 是否进入了目标 slice。典型用法：

```bash
bash runtime_monitor/scripts/check_huawei_cgroup_membership.sh \
  --slice huawei-test.slice \
  --session-dir outputs/runtime_monitor/session_cgroup_check_20260703_110252 \
  --app-pattern 'wps|WPS|qq|QQ|nautilus|Files|dde-file-manager' \
  --watch-seconds 75 \
  --interval-s 1
```

脚本会输出：

```text
outputs/runtime_monitor/<session>/review/cgroup_membership_checks.csv
outputs/runtime_monitor/<session>/review/cgroup_membership_report.md
outputs/runtime_monitor/<session>/review/cgroup_membership_processes.tsv
outputs/runtime_monitor/<session>/review/cgroup_membership_memory.tsv
outputs/runtime_monitor/<session>/review/cgroup_membership_systemctl_status.txt
outputs/runtime_monitor/<session>/review/cgroup_membership_systemd_cgls.txt
```

检查内容包括：

- `huawei-test.slice` 是否存在。
- `huawei-test.slice` 是否 active。
- `ControlGroup` 是否非空。
- `/sys/fs/cgroup/.../huawei-test.slice` 路径是否存在。
- 是否观察到 WPS、QQ、FILES 进程。
- 所有目标应用进程是否都在同一个父 slice 下。
- 是否有逃逸进程。
- `memory.max`、`memory.current`、`memory.events` 是否可读。
- `MemoryMax` 是否可通过 `systemctl --user set-property` 设置。

脚本中 `all_app_processes_same_leaf_cgroup` 只是信息项。当前实现允许不同应用位于不同 leaf scope，只要求它们都在 `huawei-test.slice` 父 cgroup 下。

## 当前验证结果示例

已验证会话：

```text
outputs/runtime_monitor/session_cgroup_check_20260703_110252
```

关键结果：

```text
final_result=PASS
ControlGroup=/user.slice/user-1000.slice/user@1000.service/huawei.slice/huawei-test.slice
memory.max=4294967296
escaped_processes_count=0
```

观察到的 leaf scope：

```text
automation-wps.scope
automation-qq.scope
automation-files.scope
```

这说明 WPS、QQ、FILES 没有处在同一个 leaf cgroup，但全部处在同一个 `huawei-test.slice` 父 cgroup 下。该结果符合当前设计。

## 与内存调度的边界

当前代码只做以下事情：

- 用 systemd user scope 放置应用进程。
- 打开 MemoryAccounting / CPUAccounting / IOAccounting。
- 读取 cgroup v2 的 memory 统计文件。
- 在验证脚本中用安全高值验证 `MemoryMax` 可设置。
- 在 Runtime Monitor 输出中记录 slice 归属和资源指标。

当前代码不做以下事情：

- 不调用预取。
- 不调用驱逐。
- 不触发 swap 策略。
- 不操作 MGLRU。
- 不写 debugfs。
- 不直接操作 page cache。
- 不对应用进程做内存调度决策。

## 常见排查命令

查看用户级 slice：

```bash
systemctl --user status huawei-test.slice --no-pager
systemctl --user show huawei-test.slice -p ControlGroup -p MemoryMax -p MemoryCurrent
```

查看 cgroup 树：

```bash
systemd-cgls --user huawei-test.slice --no-pager
```

查看 cgroup v2 内存文件：

```bash
CG="$(systemctl --user show huawei-test.slice -p ControlGroup --value)"
cat "/sys/fs/cgroup${CG}/memory.max"
cat "/sys/fs/cgroup${CG}/memory.current"
cat "/sys/fs/cgroup${CG}/memory.events"
```

验证目标应用归属：

```bash
pgrep -af 'wps|WPS|qq|QQ|nautilus|Files|dde-file-manager'
cat /proc/<pid>/cgroup
```

## 结论

当前 cgroup 设置由 automation 入口和 systemd user scope 共同完成：`run_automation.sh` 准备用户级 slice 与 GUI 环境，`app_automation.py` 用 `systemd-run --user --scope --slice=huawei-test.slice` 启动每个目标应用，Runtime Monitor 再通过 `systemctl --user show` 解析真实 cgroup 路径并读取 slice 级资源指标。

因此，当前实现的控制边界是“应用进程归属和资源观测”，不是“内存调度”。WPS、QQ、FILES 可以分属不同 leaf scope，但只要它们都位于 `huawei-test.slice` 父 cgroup 下，就满足当前实验采集链路的要求。
