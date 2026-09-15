# R1–R5：双窗口预测、内核 bin 与真实 GUI 使用效果

新增入口 `visit_window_real_effects.py`、配置 `visit-window-real-effects.json`、
适配层 `visit_window_bin.py` 均位于原 `test/test` 下。
原 M1–M5 fixture、原真实交互入口与此前 V1–V5 人工预测场景保留。
复用原 `parp-real-pc-experiment-lzx.py` 的真实 GUI 操作，以及 `app_automation.py` 的启动、
窗口校验、文件打开、视觉稳定判定和清理。未改动常驻服务的旧模型入口。

## 预测如何进入 bin

模型仍输出 `p_visit_30s` 与 `p_visit_180s`，原始概率不改写。
当前 myfs ABI 只有一个 app prior，所以新增显式投影契约 `visit_window_p180_bin_v1`：

- 后台 app prior = `ceil(p180 * 32767)`；不做 softmax，不把两个窗口相加。
- 当前前台仍用内核既有 FOREGROUND 标记和满分；这属于前台保护，不称作预测热。
- 按 p180 排序得到 rank；内核保留原有 rank 下限、bin 排序和回收逻辑。
  **写入的 prior 并不等于最终 bin 分数**，实际动作由内核统计取证。
- 模型版本501、horizon180秒、TTL最多30秒。缓存结果不重复提交，过期、未来时间、
  非单调概率、缺失的运行应用、重复 app ID、歧义 binding 均拒绝。
- 用户侧仍按 p30≥0.90 为 hot、p180<0.20 为 cold。内核冷概率门槛为6553，
  配合向上量化，不把 p180≥0.20 的应用误送入 cold-aggressive；边界有不到一个 Q15 步长的保守区间。
- 每批原子提交实际存活 cgroup binding。若应用迁出实验根 cgroup，拒绝提交并使该轮 INVALID。
- 只通过 `/dev/myfs` 提交预测；debugfs 仅用于策略控制与动作统计。

R1–R3 使用普通 bin 排序；R4–R5 另启用原内核 cold-aggressive/workload 分支，
真实 memory.stat workload hint 满足条件时，预测冷应用可越过 ordinal rank 下限。
不修改内核 rank 公式、不用人造低分冒充模型输出。全量投影见 `parp/visit_bin_projection.jsonl`。

## 行为来源与五个场景

从已有 `segments.csv` 的**独立测试分区**选片段，不读取模型输出选样本。
固定 seed 与预先声明的行为条件决定选择；保存 session、segment ID、原时间及源文件哈希。
初始5段来自原始连续历史，随后保留原始进入事件和真实停留时间。
模型时间特征使用源时间＋未经压缩的单调时钟经过时间；提交内核时将 TTL 平移到实际时钟。

| 场景 | 工作负载和观察目标 |
|---|---|
| R1 | 图片、文档、PDF、图片查看器、纸牌各准备一次后保持后台；观察其后内存及回收来源 |
| R2 | 真实测试片段在8～30秒后返回浏览器；比较可见恢复时间、refault、major fault和PSI |
| R3 | 选择历史/后续包含浏览器、邮件、视频三种应用的片段；统计8个应用的回收来源 |
| R4 | 真实图片编辑、文档保存；核验预测冷应用 file_dirty 与保护侧干净文件页的容量条件 |
| R5 | 同R4，双方设置 laptop_mode=600；PARP必须有 writepage_promotions 增量 |

R3 的筛选保证片段包含3种近期活跃应用，**不保证3个应用都被模型判热，也不伪造持续循环的访问序列**。
当前筛选条件下可用片段较少；所有实际切换均保留。五场景热名单为空时照实记录，
不能宣称已经验证“预测热应用保护”。

用户指定的8应用系统工作负载大于训练中常见的运行集合。
这里明确分离了两种条件：前台序列有独立测试数据来源，8应用运行集合是人为的系统压力条件。
因此本实验检验这类压力条件下的实际效果，不能替代原 LSApp 独立测试集的代表性准确率。

## 公平性、压力及门禁

先由应用打开/解码大图、文档、PDF、网页和音频建立工作集，继续用 GUI 打开额外图片。
压力通过实验根 cgroup 的 `memory.max` 施加，回收对象全部为这些真实应用的页面。
不使用独立分配器、MADV_COLD、fixture TOUCH/REDIRTY，也不调用 `memory.reclaim`。

每对的 Native 预压力驻留量减去固定192MiB得到边界；PARP重用完全相同的绝对边界。
双方预压力驻留量相差超过15%则 INVALID。还必须观察到至少32MiB的内存下降与 pgsteal 增量，
不能把没有回收的一轮当作效果验证。

Native 指**同一个 PARP 内核关闭优化开关**，PARP 指开启指定分支，避免同时改变内核版本。
每对共享相同 action-plan 内容、素材哈希、seed、前台事件和等待时间；私有路径不同。
记录实际动作时间，偏离原事件期限超过3秒判 INVALID。

R4/R5 的门禁使用当时模型判冷的应用，而非预先列好的冷应用：

```
cold_file_dirty >= 64 MiB
cold_clean_estimate < 192 MiB <= cold_clean_estimate + cold_file_dirty
protected_clean_estimate > 0
```

这些是 cgroup 级容量近似，不能替代页级可回收性证明。
匿名内存不冒充 file_dirty。应用自行 fsync 或后台写回造成不达标时如实记 INVALID；
R5 的 PARP 轮还必须有 `writepage_promotions > 0`。
dirty bytes/ratio、laptop_mode、PARP开关、实验内存限制均在退出时恢复。

实验期间临时停止原常驻服务，避免它迁移本轮进程或与新适配层争抢内核提交；
实验退出后恢复原服务。GUI只运行在专用Xvfb/Openbox桌面。所有进程限制与清理只针对本轮scope。

## 执行

从 PARP 根目录执行，输出目录必须不存在：

```bash
python3 test/test/visit_window_real_effects.py plan --output-dir test/outputs/visit-effects-plan
python3 test/test/visit_window_real_effects.py preflight --output-dir test/outputs/visit-effects-preflight

# Native内核上先检查GUI、数据时钟、binding编码和视觉恢复；不施加内存压力、不提交ioctl
python3 -u test/test/visit_window_real_effects.py gui-check --scenario r2 \
  --output-dir test/outputs/visit-effects-gui-check

# 在支持 myfs v3、bin、cold-aggressive、workload 的 PARP 内核上执行
python3 -u test/test/visit_window_real_effects.py run \
  --output-dir test/outputs/visit-effects-kernel
```

初始顺序R2、R3、R1、R4、R5，每场景一对，不默认重复三轮。
独立场景 INVALID 后保留证据并继续下一场景；不因准确率低而重跑或修改门槛。
GUI_CHECK_PASS 只表示链路验证成功，不作为性能结果。

## 报告与指标解释

`REPORT.md`、`summary.json`逐轮更新；每轮保留以下文件：

- `action-plan.json`、`asset_hashes.json`、`verified-actions.json`：计划、素材与实际动作。
- `memory-timeline.jsonl`、`before-pressure.json`、`after-pressure.json`：cgroup驻留、anon/file、swap、fault与PSI。
- `return-probes.json`：切换开始到页面变化并连续3帧稳定的时间；另存命令完成时间。
- `kernel-before.json`、`kernel-after.json`、`kernel-delta.json`：预测支持的bin动作及writepage promotion。
- `dirty-capacity-gate.json`、`pressure-boundary.json`、`controls-*.json`：容量和状态恢复证据。
- `parp/`：myfs审计、投影与workload取证；`prediction-evaluation.json`：双窗口标签指标。

预测标签来自独立观测的实际前台进入，持续停留不算访问；后续观察不足的负标签排除。
单应用 pswpin 在内核未提供相应 cgroup 计数时记为 null；不会以全系统 vmstat 冒充单应用指标。
初始一对只用于验证链路与发现效果，不足以得出稳定收益或统计显著性结论。
