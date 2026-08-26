# Linux 6.17 参考配置的内存调度实验设计

本文固定 6.17 是为了论文复现时控制变量，不是 `memsched_exp` 测量核心的运行依赖。
核心模块可以在其他支持所需 `/proc`、cgroup v2 和 tracepoint 能力的 Linux 内核上运行。

## 1. 研究目标与论文对齐

本实验不是把 Android 命令机械搬到桌面 Linux，而是复现三篇论文的压力结构和可比指标：

- **Acclaim**：关注 refault、direct reclaim、前后台优先级和回收粒度。原文在 3 GB Huawei P9、Linux 4.1.18 上使用 0/3/8/15 个后台应用，前台 app 使用 5 分钟，每组 10 次；2.5 GB 场景用 memtester 占内存。评价页还使用 AngryBirds 5 分钟、512 MB/1 GB 的 4 KB I/O、启动延迟和 FPS。
- **AppFlow**：关注 >1 GB 应用冷启动中的 I/O 与内存回收耦合。原文低/中/高负载分别为 5 个小应用、15 个小应用、15 个小应用加 2 个 GB 级应用；每次先杀后台应用并清 file cache，再建立后台负载并启动目标。指标为冷启动、冷重启次数、I/O throughput、direct reclaim 和 LMK。
- **Fleet**：关注 managed-runtime GC 与 swap 的冲突。原文 Pixel 3/4 GB、2 GB flash swap；合成 app 的对象大小为 512 B 或 2048 B、每 app 180 MB；商业 app round-robin 两轮，每 app 使用 30 秒；高压下约 10 个后台 app，每个 app 20 次热启动，两次之间使用其他 app 30 秒；帧实验前台运行 1 分钟并持续滑动。

Linux QQ/WPS 首轮是基线验证，不替代论文的 Android/ART 数据。其目的为确认 Linux 6.17 的采集链路、窗口启动、cgroup、I/O、CPU、refault 和 direct reclaim 都能产生一致、可追溯的记录。

## 2. 测试机设置

### 2.1 必需条件

1. 独占的 x86_64 Linux 6.17.x 测试机，固定 BIOS、电源模式和散热条件。
2. cgroup v2 unified hierarchy；systemd user manager 可用。
3. 建议使用 X11 会话完成第一轮，因为 `wmctrl` 能观察首个 mapped window。Wayland 原生窗口使用 ready-file/compositor probe。
4. bpftrace、bpftool、clang/llvm、Python 3.10+、jq、sysstat、fio、stress-ng、wmctrl、xdotool。
5. 独立测试用户；QQ/WPS 登录状态、文档、字体、插件、网络条件固定。

运行 `bash scripts/preflight.sh`。任何 kernel/cgroup `FAIL` 必须先处理；tracepoint 缺失可使用 vmstat fallback，但该轮不能报告 eBPF direct-reclaim 精确次数。

### 2.2 固定变量

每轮由 `metadata.json` 自动记录：

- `uname -a`、内核 config、CPU 型号/核数、内存容量、NUMA、存储型号/文件系统；
- swap/zram 的设备、大小、优先级和压缩算法；
- `vm.swappiness`、watermark scale/boost、overcommit、THP 模式；
- CPU governor、频率约束、桌面环境、X11/Wayland、显示刷新率；
- QQ/WPS 解析后的可执行文件路径、SHA-256、Debian 包名和版本；
- 是否清 file cache、是否重启、是否联网、WPS 测试文件哈希。

比较不同内存策略时，以上参数除被测策略外必须完全相同。每个正式场景至少 10 次；当前配置按用户要求先执行 QQ/WPS 各 1 次冷启动冒烟，可用 `COLD_REPETITIONS=5` 扩展。用固定随机种子生成执行顺序，但 baseline 与 candidate 要交错，而不是先测完一种再测另一种。

### 2.3 内存容量与压力

优先用独立测试机的 boot 参数或包住整个场景的 systemd/cgroup 限额，不建议用宿主机虚拟化 balloon 临时变化。Acclaim/Fleet 默认拒绝总内存超过 4 GiB 的未限额主机，AppFlow 默认拒绝超过 8 GiB；`ALLOW_UNCONSTRAINED_MEMORY=1` 只用于非论文对齐冒烟。建立三个可重复级别：

- **充足**：目标 app 启动前 `MemAvailable` 大于目标 working set 的 1.5 倍；
- **中压**：后台 15 个 100 MB worker，或调参使 memory PSI `some.avg10` 持续非零；
- **高压**：15 个小 worker 加 2 个 1 GB worker，并保证系统仍可响应。先逐步升压找到安全值，禁止在生产机运行。

不要用最终发生 OOM 的容量作为所有轮次默认值；OOM 是单独边界实验。

## 3. QQ/WPS 第一轮

### 3.1 安装

在目标机执行：

```bash
bash scripts/install_qq_wps_ubuntu.sh
```

脚本只接受 QQ 和 WPS 官方页面解析出的包，或用户从官方页面复制并通过 `QQ_DEB_URL`/`WPS_DEB_URL` 传入的直链。不要把第三方镜像固化为实验依赖。首次登录并完成初始化后重启应用一次，关闭自动升级和无关弹窗。

### 3.2 固定动作

QQ：登录后固定进入同一个本地会话，滚动聊天记录 20 秒，再静置到 60 秒结束。禁止每轮接收不同文件或视频。

WPS Writer：打开同一个本地 DOCX（建议 100 页、含固定图片），等待首窗口后滚动 20 秒，再搜索固定字符串一次，静置到 60 秒。网络字体与云文档关闭。

### 3.3 冷启动轮次

1. 终止本用户的目标进程，等待 3 秒并确认无残留子进程。
2. “进程冷”只要求无进程；“严格冷缓存”额外执行 `sync` 与 drop_caches。两类结果分开存放。
3. 预创建应用 cgroup；collector 先写入 before 快照和 collector-ready marker，应用等待系统/cgroup collector 均 ready 后才写 start marker 并启动。
4. X11 记录首 mapped window；若可修改 wrapper，则在首帧可交互时 `touch READY_FILE`，以该值为主。
5. 按固定动作操作到 60 秒，保存 raw snapshots、launch JSON 和 eBPF events。
6. 当前首轮 QQ、WPS 各 1 次；链路验证后各 5 次冒烟，正式数据各至少 10 次。

命令：

```bash
bash scripts/run_qq_wps_round.sh
# 仅隔离测试机执行严格冷缓存：
DROP_CACHES=1 bash scripts/run_qq_wps_round.sh
```

### 3.4 热启动轮次

保持原进程存活，把应用切到后台 30 秒，期间激活另一个固定应用，然后重新激活目标。必须记录切回前后的 PID start time；只有同一进程仍存活才计为热启动。执行 5 次冒烟、20 次正式轮次。Wayland 下需要 app/compositor 首帧标记，否则只报告 activation-to-window proxy。

## 4. Acclaim 对齐场景

### 4.1 负载矩阵

| 场景 | 后台数 | 每后台默认 footprint | 前台时长 | 重复 |
|---|---:|---:|---:|---:|
| A | 0 | - | 300 s | 10 |
| 3B+A | 3 | 180 MB | 300 s | 10 |
| 8B+A | 8 | 180 MB | 300 s | 10 |
| 15B+A | 15 | 180 MB | 300 s | 10 |

执行 `bash scripts/scenarios/run_acclaim.sh {0,3,8,15}`。默认 foreground 是 256 MB 匿名页 worker；可把第二参数替换为固定游戏、QQ 或 WPS 命令。后台先启动并稳定 10 秒，collector 与前台阶段同时开始。

### 4.2 采集与判定

- 系统 refault count/ratio、direct reclaim 精确事件、allocstall、direct/kswapd scanned-page ratio；
- 前台置于独立 cgroup 后，采集 foreground refault；
- memory PSI、CPU、I/O、OOM kill；
- 前台为游戏时收集 frame CSV；
- 另做 512 MB/1 GB、4 KB block 的读写微基准，保持存储与 cache 状态一致。

Acclaim 的 Android LMK 不映射到 Linux OOM；二者并列报告。

## 5. AppFlow 对齐场景

### 5.1 压力级别

| 级别 | 小应用 | GB 级后台应用 | 目标 |
|---|---:|---:|---|
| low | 5 × 100 MB | 0 | 1.2 GiB cold read/app |
| medium | 15 × 100 MB | 0 | 同上 |
| high | 15 × 100 MB | 2 × 1 GiB | 同上 |

执行：

```bash
bash scripts/scenarios/run_appflow.sh low
bash scripts/scenarios/run_appflow.sh medium
bash scripts/scenarios/run_appflow.sh high
```

脚本第一次创建 1.2 GiB 文件：逐块写入确定性伪随机内容、`fsync`，并生成 `.meta.json` SHA-256 manifest；旧版稀疏文件没有有效 manifest 时会自动重建。每轮先执行逐文件 `POSIX_FADV_DONTNEED`，再用 `mincore` 证明驻留率不高于 1%（阈值可配置），验证失败则拒绝运行。`DROP_CACHES=1` 只是独占测试机上的可选全局补充。默认 128 KB block 表示 AppFlow “during-launch throughput-first”方向；再用 4/16/32/64/128/256/512 KB 做 block-size sweep，报告吞吐与延迟曲线。

### 5.2 真实 GB 级应用

合成读验证链路后，选择实际 working set/资源文件超过 1 GiB 的游戏、本地模型或媒体应用。每轮：杀掉所有背景测试 app、清目标 cache、建立压力、启动目标、到首帧停止 launch timer，继续记录 60-120 秒。记录冷重启次数、背景存活率、direct reclaim、OOM、cgroup I/O throughput。安装包大小不能替代实际读取 working set。

## 6. Fleet 对齐场景

### 6.1 合成对象负载

`bash scripts/scenarios/run_fleet.sh 512 18` 和 `... 2048 18` 对齐 Fleet 的小/大对象、180 MB/app。每个 JVM 完成热阶段后继续驻留，直到 orchestrator 结束或远大于实验窗口的 hold timeout，因此存活数不会混入 30 秒自然退出。每增加一个 app 都等待 ready marker，再记录已启动数和仍存活数；首次未 ready、kill/OOM 或存活数下降前的最大值为缓存容量。

Java workload 输出的 `object_reaccess_ratio` 定义为热阶段访问的 distinct objects 中，前台阶段已经访问过的 distinct objects 比例。这只是可重复的 app-level proxy；没有修改 ART/JVM 时不得称为 GC 内部对象重访问。

### 6.2 商业应用 round-robin

QQ/WPS 加上浏览器、媒体和工具类应用，固定顺序运行两轮，每 app 前台 30 秒。第一轮建立后台集，第二轮判定原进程存活、cold/hot 类型和延迟。正式 Fleet 对齐需要约 10 个背景 app，每目标 20 次 hot launch，间隔使用其他 app 30 秒。

### 6.3 GC、帧与 CPU

- GC working set 必须通过修改 runtime 或 JVMTI/ART probe 统计“一次 GC 扫描对象数”；`perf` page faults 不是对象数。
- 帧实验每 app 前台 60 秒，执行完全相同的滑动脚本；60 Hz 下 duration >16.7 ms 为 jank。
- 用 `python3 -m memsched_exp.cli frames --csv frames.csv --output RUN/frames-summary.json` 计算 FPS、每秒 FPS 标准差和 jank ratio。
- CPU 使用独立 cgroup `cpu.stat`，同时报告 one-core equivalent 与 machine share。
- Java heap ratio仅对 JVM/ART app 计算；QQ/WPS 为 `N/A`。

## 7. eBPF 与 XVM 对照

按手册截图的概念关系：

| XVM/鸿蒙术语 | 本项目 Linux eBPF 实体 |
|---|---|
| XVM | eBPF VM/verifier/JIT 执行环境 |
| xvm module | `bpf/reclaim.bt` 中的 eBPF program |
| HOST | Linux kernel tracepoint 与 bpftrace runtime |
| CLIENT | `scripts/run_bpf_collector.sh` 用户态 loader |
| helper | bpftrace/eBPF helper 与内建变量 |
| MAP | `@direct_start[tid]` BPF map |
| ring buffer | bpftrace 的内核到用户态事件缓冲与 JSONL 输出 |
| TRACE | `vmscan:*`、`oom:*` tracepoint |
| uprobe | 首帧需要时可对 toolkit present/commit 函数挂 uprobe；首轮未硬编码不稳定库符号 |
| hmprobe | 对应统一探针管理层；本项目由 runner + tracepoint 清单承担 |
| libhmpsf | 对应 libbpf/bpftrace 用户态装载与消费层 |

本项目只用 tracepoint，不用易随内核构建变化的 reclaim kprobe 符号。启动前由 preflight 检查 Linux 6.17 当前 tracepoint 是否存在。`@direct_start` 以 TID 配对 begin/end，事件通过 JSONL 进入用户态；这与 XVM module + MAP + ring buffer + CLIENT 的结构一一对应。

## 8. 统计与结果验收

1. 每场景至少 10 次；热启动按 Fleet 为 20 次。
2. 报 median、mean、standard deviation、p90/p95；延迟优先 median/p90，FPS 同时报均值和标准差。
3. baseline/candidate 用配对轮次和相同动作；报告配对差与 bootstrap 95% CI。
4. 任一轮发生升级弹窗、登录失效、网络内容变化、热降频或人为操作偏离，标为 invalid，不悄悄删除。
5. refault ratio 分母为 0 时输出 null，不填 0。
6. runner 等待 eBPF `collector_start` 后才放行负载；BPF begin/end 丢配对、collector stderr 有 lost/dropped events、cgroup 端点消失或 inode 改变时自动标记无效，但 vmstat raw data 仍保留。
7. 先完成 QQ/WPS 各 1 次首轮并检查 raw→summary 一致，再扩为 5 次冒烟和论文工作负载。
8. 正式轮次必须生成 schema-v4 `manifest.json`；比较器按场景、seed、repetition、cache state 和 workload hash 配对，并拒绝环境指纹不一致的轮次。
9. 使用 `memsched_exp.compare` 生成 mean、median、sample standard deviation、p90/p95、配对差、改善率和 bootstrap 95% CI；Baseline 为 0 时改善率保持 `null`。
