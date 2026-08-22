# Bilibili 应用内 cgroup v2 细分

本项目在 Linux cgroup v2 中把 Bilibili Desktop 的 Electron 多进程结构细分为
独立功能组，并在应用运行期间持续识别、迁移动态产生的进程。

```text
bilibili systemd service
├── audio
├── gpu
├── network
├── renderer
└── other
```

当前实现只针对 Bilibili，不是通用的多应用 cgroup 管理框架。

## 当前状态

该方案已经完成真实 cgroup 创建和进程迁移验证，不只是概念设计：

- Bilibili 被确认是 Electron 多进程应用。
- 5 次完整冷重启后，GPU、NetworkService 和 renderer 的分类规则保持稳定。
- renderer 会动态创建和销毁，因此管理器采用持续扫描，而非一次性分类。
- 用户级 transient systemd service 成功获得 `Delegate=yes`。
- `cpu`、`memory`、`io`、`pids` 控制器成功下放到五个叶子组。
- 原型测试完成 13 次真实迁移，`move_error=0`。
- 实际视频播放期间，25 个逐秒样本中有 14 次捕获到
  `Chromium / Playback` 音频流；音频流 PID、AudioService PID 和 `audio`
  cgroup 归属一致。

完整验证过程和边界参见 [FINDINGS.md](FINDINGS.md)。

当前仍属于可运行的 transient 原型：应用启动时创建 cgroup，退出后由 systemd
清理。尚未替换系统桌面启动项，也没有为子组设置正式 CPU、内存或 I/O 限制。

## 已验证环境

- Ubuntu，Linux 6.17.13
- cgroup v2 unified hierarchy
- systemd 用户会话支持 `Delegate=yes`
- Wayland + XWayland
- Bilibili Desktop `io.github.msojocs.bilibili` 1.17.6
- Electron 28.2.1
- Python 3.10+

当前代码使用以下本机安装路径：

```text
/opt/apps/io.github.msojocs.bilibili/files/bin/bin/bilibili
```

同时有部分路径固定为 `/home/wency/cgroup_setting`。在其他用户或安装方式下使用
前，应先把这些路径改成实际位置。

## 进程分类规则

| cgroup | 稳定识别依据 |
|---|---|
| `audio` | `--utility-sub-type=audio.mojom.AudioService` |
| `gpu` | `--type=gpu-process` |
| `network` | `--utility-sub-type=network.mojom.NetworkService` |
| `renderer` | `--type=renderer` |
| `other` | 无法匹配以上规则的主进程、zygote 和启动器 |

规则的机器可读版本位于 [bilibili_roles.json](bilibili_roles.json)。不要使用固定 PID
分类；PID 在每次启动后都会改变。

当前不能可靠判断多个 renderer 中哪个是唯一“前台 renderer”，因此所有 renderer
统一进入 `renderer`，不做未经证据支持的前后台细分。

## 文件说明

建议上传到 GitHub 的核心文件：

```text
README.md
FINDINGS.md
bilibili_roles.json
inspect_app.py
manage_bilibili_cgroups.py
launch_bilibili_cgroups.sh
validate_restarts.py
```

各文件用途：

- `manage_bilibili_cgroups.py`：创建叶子 cgroup、启动应用、持续分类和迁移进程。
- `launch_bilibili_cgroups.sh`：使用 `systemd-run --user` 和 `Delegate=yes` 启动管理器。
- `inspect_app.py`：只读采集应用进程树、命令行角色、cgroup 和音频流 PID。
- `validate_restarts.py`：执行多轮冷启动，验证分类规则跨 PID 是否稳定。
- `bilibili_roles.json`：当前已验证的角色规则。
- `FINDINGS.md`：实验过程、真实数据、已知限制和最终结论。

`CONTINUE_PROMPT.md` 是后续开发交接文档，可上传到 `docs/`，但不是运行所必需。

以下文件不应作为核心源码上传：

```text
__pycache__/
*.pyc
restart-*.log
initial-snapshot.jsonl
```

其他 `*.jsonl` 是包含瞬时 PID 和本机 cgroup 路径的原始验证证据。若需要公开实验
复现材料，可选择性放入 `evidence/`；若只发布工具和结论，则无需上传。

## 快速使用

确认当前会话使用 cgroup v2：

```bash
stat -fc %T /sys/fs/cgroup
```

预期输出为 `cgroup2fs`。

确保脚本可执行：

```bash
chmod +x inspect_app.py manage_bilibili_cgroups.py \
  validate_restarts.py launch_bilibili_cgroups.sh
```

使用细分 cgroup 启动 Bilibili：

```bash
./launch_bilibili_cgroups.sh
```

启动脚本会输出 transient systemd unit 名称和本轮迁移审计 JSONL 路径。查看服务：

```bash
systemctl --user status 'bilibili-sliced-*.service'
```

查看某个运行中进程的实际归属：

```bash
cat /proc/PID/cgroup
```

应用退出后，transient unit 和 cgroup 会自动清理，迁移审计文件保留。

## 只读勘察

启动 Bilibili 并播放视频后运行：

```bash
python3 inspect_app.py \
  --samples 30 \
  --interval 2 \
  --output bilibili-playing.jsonl
```

该工具只读取 `/proc` 和 PulseAudio/PipeWire 信息，不创建或迁移 cgroup。

## 设计约束

- cgroup 的实际管理粒度主要是进程；同一进程内部的音频、显示和网络线程不能通过
  普通 domain cgroup 分别统计内存。
- 父节点只负责组织和总体控制，进程放入叶子 cgroup。
- AudioService 和 renderer 都可能按需创建或重建，分类器必须持续运行。
- `other` 包含 Electron 主进程和 zygote，不能假定它是不重要的后台负载。
- 当前没有设置 `memory.max`、`memory.high`、`cpu.max` 等资源限制；分组成功不等于
  已经找到最佳资源策略。
- 后续资源实验必须先记录无资源限制基线，每次只修改一个变量，并观察音频中断、
  视频卡顿、renderer 重建、PSI 和 OOM 事件。

## 安全与回滚

- 当前实现不修改 `/usr/share/applications` 中的软件包桌面文件。
- 当前实现不修改 GRUB、内核启动参数或系统级 cgroup 配置。
- cgroup 由 transient systemd unit 创建，应用退出后自动删除。
- 停止对应用户服务即可终止该轮受管应用并清理 cgroup：

```bash
systemctl --user stop UNIT.service
```

- 不要在未测量峰值前给 `audio`、`gpu` 或主进程设置严格内存上限。

## 后续工作

1. 加固管理器的异常退出、PID 重用和迁移后验证逻辑。
2. 增加分类和异常路径自动化测试。
3. 建立用户级 systemd/desktop entry 集成，同时保留原始启动回滚入口。
4. 先采集五个子组的 CPU、内存、I/O 和 PSI 基线。
5. 在基线稳定后逐项实验资源权重或软限制。

详细交接顺序可参考 `CONTINUE_PROMPT.md`。
