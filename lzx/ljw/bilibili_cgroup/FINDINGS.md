# Bilibili 进程角色勘察结果

采集日期：2026-08-21。系统为 Linux 6.17.13、cgroup v2、Wayland。

## 已确认

当前安装包 `io.github.msojocs.bilibili` 版本 1.17.6，基于 Electron 28.2.1。
启动快照中共发现 11 个应用进程，其中功能角色如下：

- 独立 GPU 进程：命令行包含 `--type=gpu-process`。
- 独立网络进程：包含 `network.mojom.NetworkService`。
- 独立音频服务进程：包含 `audio.mojom.AudioService`。
- 4 个独立 renderer：均包含 `--type=renderer`。
- 启动脚本、Electron 主进程和 zygote 暂归入 `other`。

这些角色参数由 Electron/Chromium 直接写入命令行，比 PID、进程名和瞬时 CPU
使用率更适合作为自动分类依据。对应规则保存在 `bilibili_roles.json`。

## 当前适合的 cgroup 层级

```text
bilibili/
├── audio/
├── gpu/
├── network/
├── renderer/
└── other/
```

目前不应把某一个 renderer 标记为“前台”：同一时间存在多个同名 renderer，启动
快照没有稳定的页面/窗口标识。若研究目标只要求功能组件资源隔离，使用统一的
`renderer` 组已经足够且可审计。

## 仍需研究（非当前方案阻塞项）

1. 若必须区分前台 renderer，需要结合窗口/页面生命周期或应用级探针，而不能只看
   `--type=renderer`。

`bilibili-startup.jsonl` 是本次原始快照，不能用其中的具体 PID 作为未来规则。

## 后续验证结果

### 运行态生命周期

连续 15 秒采样中，AudioService、GPU 和 NetworkService PID 保持不变；renderer
从启动快照的 4 个自然减少到 2 个。这证明 renderer 会动态创建和销毁，分类程序
必须持续运行，不能只在应用启动时扫描一次。原始数据为 `bilibili-runtime.jsonl`。

该窗口内没有活动的 PulseAudio/PipeWire sink input；后续实际播放验证见下文。

### 五次冷重启

五轮完整退出和重启均得到相同的启动角色结构：

| 轮次 | other | gpu | network | renderer |
|---:|---:|---:|---:|---:|
| 1 | 4 | 1 | 1 | 4 |
| 2 | 4 | 1 | 1 | 4 |
| 3 | 4 | 1 | 1 | 4 |
| 4 | 4 | 1 | 1 | 4 |
| 5 | 4 | 1 | 1 | 4 |

PID 每轮均变化，但命令行分类证据不变。AudioService 在首页启动后的 8 秒采样点
尚未按需创建，因此表中没有 audio；它在播放器生命周期内出现。原始数据为
`restart-validation.jsonl`，每轮应用日志为 `restart-01.log` 至 `restart-05.log`。

### 实际 cgroup v2 原型

用户级 transient systemd service 成功获得 `Delegate=yes`。委派目录归用户
`wency` 所有，可写 `cgroup.procs` 和 `cgroup.subtree_control`，不需要 root。

12 秒原型测试成功启用 `cpu memory io pids` 四个控制器，并完成以下迁移：

- audio：1 个；
- gpu：1 个；
- network：1 个；
- renderer：5 个（包含运行中动态产生的 renderer）；
- other：5 个。

总计 13 次迁移，`move_error=0`。这同时验证了 AudioService 按需出现后能被持续
分类器捕获，以及新 renderer 能被重新归类。原始迁移审计记录为
`cgroup-test-events.jsonl`。

结论：应用内 cgroup 细分在本机已经从“理论可行”推进到“实际创建和迁移成功”。
当前可正式使用 `audio/gpu/network/renderer/other` 五组结构。

### 实际视频音频流交叉验证

第一次 URL 启动采样只出现 Electron 主进程产生的 `dialog-warning` 提示音：音频流
PID 5934，媒体名 `dialog-warning`。它不是视频音轨，不能用于验证 AudioService。
这也证明仅看到任意 sink input 就下结论会造成误判。原始数据为
`audio-validation-samples.jsonl` 和 `audio-validation-migrations.jsonl`。

随后关闭登录弹窗，在主窗口显式触发视频播放。25 次、间隔 1 秒的样本中，有 14
次捕获到真实播放流：

- `application.name`：`Chromium`；
- `media.name`：`Playback`；
- PulseAudio/PipeWire 报告的客户端 PID：6497；
- 同一 PID 的命令行角色：`audio.mojom.AudioService`；
- 同一 PID 的实际 cgroup：
  `bilibili-audio-interaction.service/audio`。

14 次有效观测的 PID 与 cgroup 均一致。因此音频角色规则通过了“命令行标记、实际
音频流、最终 cgroup 归属”三方交叉验证。原始逐秒证据为
`audio-playing-samples.jsonl`，迁移记录为 `audio-interaction-migrations.jsonl`。

最终结论：`audio`、`gpu`、`network`、`renderer`、`other` 五组分类均有实际进程
证据；其中音频组还具有真实播放流证据。当前方案不存在必须先完成的验证项，可以
进入资源策略配置和长期实验阶段。
