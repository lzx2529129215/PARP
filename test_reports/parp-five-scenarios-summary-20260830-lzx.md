# PARP 五个预测回收场景总结报告

日期：2026-08-30  
项目：Linux 6.17.13 Native 与 PARP LSTM/reclaim-bin/cold-aggressive 机制验证  
报告性质：汇总现有五个受控场景的目标、自动化动作、指标、结果和适用边界。<!-- lzx-note -->

## 一、总体结论

现有五个场景已经形成一条由浅入深的验证链：

1. 场景一验证预测冷应用是否会被优先回收。
2. 场景二验证即将复用的应用能否被保护，并把回收选择转化为更低的 refault、major fault 和恢复延迟。
3. 场景三验证多个热、冷应用并存时，固定回收量究竟来自哪些应用。
4. 场景四验证能否用预测冷应用的冷脏页替代热应用的干净冷页，同时检查无条件 cold-aggressive 是否会过度加压。
5. 场景五补齐场景四未满足的写回门禁，验证 cold-aggressive 在确实需要提前促进冷脏页写回时是否有增量收益。

当前可以得到的工程结论是：

- **LSTM + reclaim-bin 的机制和用户复用收益均已在训练集对齐场景中验证通过。**它可以把相同回收任务从预测热应用迁移到预测冷应用，并显著减少随后复用热应用产生的文件页 refault、major fault、读取量和延迟。
- **cold-aggressive 不是默认收益项，而是条件性策略。**场景四证明在 bin-only 已足够时继续加压会增加扫描、swap 和 PSI；场景五证明当冷干净页不足、冷脏页足够、热页面临回收且初始写回受限时，它能够获得增量收益。
- 五个场景属于“训练序列对齐的受控机制实验”。它们证明预测正确时内核路径有效，但不能直接代表训练集外任意真实桌面序列的平均收益。

## 二、五个场景的共同实验基础

### 2.1 应用集合

共使用 8 个免登录、可自动化的真实 Linux GUI 应用：

| 角色 | 应用 |
|---|---|
| 预测热应用 | Firefox、Thunderbird、VLC |
| 预测冷应用 | GIMP、LibreOffice、Evince、Image Viewer、Solitaire |

受控工作集 fixture 与真实 GUI 应用使用相同 App ID，并位于对应 GUI scope 内。因此 runtime service 预测、内核实际回收和测试采集面对的是同一个应用 cgroup，不存在 fixture scope 与 GUI scope 分离的问题。

### 2.2 训练集对齐序列

五个场景的最终 LSTM 历史统一为：

```text
Thunderbird -> Firefox -> Thunderbird -> Firefox -> VLC
```

该序列下 Firefox、Thunderbird 是高重入概率应用，其余五个应用为低概率应用。每个正式轮次都经过预测门禁，验证历史、当前应用、热应用排名、冷应用概率、App ID 绑定和 `/dev/myfs` 下沉状态；不满足门禁的轮次标记为 `INVALID`，不能进入统计。

### 2.3 对照原则

- Native：未包含 PARP 代码的 Linux 6.17.13。
- PARP OFF：相同 PARP 内核但运行时优化关闭，用于判断编译差异是否影响结果。
- bin-only：只开启 LSTM + reclaim-bin，关闭 effective-tier、Tier2、WSS 和 cold-aggressive。
- cold-aggressive：在 bin-only 基础上开启预测冷应用增强扫描和 workload-aware 页面类型策略。
- 配对组使用相同应用、页面容量、前台切换序列和 seed。

需要注意：场景一至三的正式结果来自 r7，场景四、五来自后续修正后的 r11。不同场景的回收目标和页面布局也不同，因此各场景数值用于回答各自假设，不能简单横向相加。

## 三、场景一：启动后不再复用的冷应用

### 3.1 目的与现实含义

模拟用户打开过一些应用，随后转去使用另一组应用，前面的应用在当前会话中不再返回。例如查看完图片或 PDF 后长期编辑文档、浏览网页或播放视频。

要验证的是：在必须释放内存时，PARP 能否比 Native 更稳定地优先收回这些“使用过一次、后续不再复用”应用的冷页面。

### 3.2 自动化动作

1. 启动 8 个真实 GUI 应用，并为每个应用在同一 App ID scope 内建立 256 MiB 文件工作集，其中 32 MiB 为前台热区。
2. 依次切换到 GIMP、LibreOffice、Evince、Image Viewer、Solitaire，使五个冷应用都真实使用一次。
3. 后续不再切回这五个应用。
4. 执行五步训练集对齐序列，并在每次前台切换时触碰相应应用的 32 MiB 热区。
5. runtime service 根据窗口事件运行 LSTM，并通过 `/dev/myfs` 下沉应用概率和 cgroup 绑定。
6. 采集压力前逐应用驻留快照。
7. 在实验父 cgroup 内申请 1024 MiB 压力内存，通过 `memory.max` 形成约 768 MiB 的定向回收需求。
8. 压力进入 `HOLDING` 后采集逐应用驻留下降、direct reclaim、PSI 和内核 bin 动作。

### 3.3 主要指标

- 五个冷应用贡献的回收量和占比。
- 三个热应用的误回收量。
- `pgscan_direct`、`pgsteal`。
- reclaim-bin 的 `subtree_selected`、`subtree_skipped` 等实际动作。

### 3.4 结果与结论

三轮中位数：

| 指标 | Native | PARP OFF | LSTM + bin |
|---|---:|---:|---:|
| 冷应用回收占比 | 50.26% | 36.65% | **99.03%** |
| 冷应用回收量 | 388.0 MiB | 283.64 MiB | **768.5 MiB** |
| 热应用误回收量 | 386.0 MiB | 490.38 MiB | **7.5 MiB** |

结论：场景一通过。预测冷应用确实成为主要回收来源，且结果比 Native/OFF 的原生遍历顺序更集中、更稳定。

限制：场景一和场景三目前共用相同压力动作骨架。场景一强调生命周期语义，场景三强调精确来源统计；它们不是完全独立的两种负载。

## 四、场景二：即将返回应用的“应用级热、页面级冷”保护

### 4.1 目的与现实含义

模拟用户在浏览器、邮件和播放器之间切换。Firefox 暂时在后台，其部分页面按照传统 LRU 观察已经变冷，但 LSTM 根据应用切换历史预测用户很快会返回 Firefox。

这里的“热”是应用级未来复用概率高；“冷”是页面近期没有访问。目标是保护这类页面，避免用户切回应用时重新从文件系统读取。

### 4.2 自动化动作

1. 与场景一相同，启动 8 个应用并准备每应用 256 MiB 文件工作集和 32 MiB 前台热区。
2. 五个冷应用各使用一次后退出前台活动序列。
3. 执行训练序列 `Thunderbird -> Firefox -> Thunderbird -> Firefox -> VLC`，最后停留在 VLC。
4. LSTM 在最后一次 VLC 事件后预测 Firefox、Thunderbird 为高概率重入应用，并通过 `/dev/myfs` 下沉。
5. 在 Firefox 位于后台、其大部分页面不再被访问时，制造约 768 MiB 定向回收压力。
6. 压力快照完成后真实切回 Firefox。
7. 精确触碰 Firefox 文件中偏移 32 MiB、长度 128 MiB 的页面区间。
8. 只对“压力结束到首次触碰完成”窗口统计 refault、major fault、direct reclaim、读取延迟；压力阶段 fixture 建立产生的 fault 不计作复用收益。

### 4.3 主要指标

- Firefox 压力后的文件页驻留量。
- 首次复用 `workingset_refault_file`。
- `pgmajfault`、`pgfault`。
- 复用窗口 direct reclaim。
- 128 MiB 逐页触碰延迟。

### 4.4 结果与结论

三轮中位数：

| 指标 | Native | PARP OFF | LSTM + bin | 相对 Native |
|---|---:|---:|---:|---:|
| Firefox 文件页回收 | 大部分 | 大部分 | **0 MiB** | 完整保护 |
| 128 MiB file refault | 35,840 | 35,840 | **0** | -100% |
| major fault | 1 | 1 | **0** | -100% |
| 复用 direct scan | 52,321 | 52,224 | **16,640** | -68.20% |
| 首次复用耗时 | 95.834 ms | 117.027 ms | **56.850 ms** | -40.68% |

`pgfault` 只下降约 1.87%，原因是首次建立 128 MiB 页表映射本身会产生约 32,768 次 minor fault，即使文件页仍驻留也无法消除。这个场景中更能体现错误回收代价的是 file refault、major fault、读取 I/O 和复用延迟。

结论：场景二通过。它把“回收方向改变”进一步转化成了用户返回预测热应用时的可测收益。

## 五、场景三：多热应用与多冷应用的回收来源分布

### 5.1 目的与现实含义

模拟真实桌面同时保留多个应用：用户持续在浏览器、邮件和播放器之间切换，同时还挂着图片编辑器、办公套件、PDF 阅读器、看图工具和休闲游戏，但短期内不会返回后五个应用。

当系统必须回收固定容量时，要验证 PARP 是否主要从预测冷应用取内存，而不是像 Native 一样按原生遍历/LRU 状态同时牺牲热、冷应用。

### 5.2 自动化动作

1. 启动 3 个预测热应用和 5 个预测冷应用；每个应用准备 256 MiB 文件工作集及 32 MiB 前台热区。
2. 五个冷应用先依次使用一次，之后只在 Firefox、Thunderbird、VLC 相关的训练序列内切换。
3. 等待 LSTM 推理和 `/dev/myfs` 下沉完成，并验证 8 个 App ID/cgroup 绑定。
4. 压力前记录每个应用工作集的驻留量。
5. 在相同父 cgroup 内制造约 768 MiB 回收目标。
6. 压力达到 HOLDING 后，再次记录每个应用驻留量。
7. 按应用计算回收来源分布，比较 Native、PARP OFF 和 bin-only。

### 5.3 主要指标

- 冷/热应用回收占比。
- 每个应用的精确回收 MiB。
- 总回收量是否相当。
- direct scan 是否因为“回收更多”而产生伪收益。

### 5.4 结果与结论

三轮中位数：

| 指标 | Native | PARP OFF | LSTM + bin |
|---|---:|---:|---:|
| 冷应用回收占比 | 50.13% | 50.13% | **99.03%** |
| 冷应用回收量 | 388.0 MiB | 388.0 MiB | **768.5 MiB** |
| 热应用回收量 | 386.0 MiB | 386.0 MiB | **7.5 MiB** |
| 总文件驻留回收 | 774.0 MiB | 772.0 MiB | 776.0 MiB |
| direct scan | 199,088 | 198,744 | 198,665 |

LSTM + bin 的典型逐应用回收中，Firefox、VLC 为 0 MiB，Thunderbird 约 7.5 MiB；GIMP、LibreOffice、Evince 各约 242 MiB。总回收任务基本等量，扫描量也近似，因此收益来自回收对象重排，而不是少完成了回收任务。

结论：场景三通过。PARP 能把回收来源从约一半热、一半冷，调整为约 99% 来自预测冷应用。

## 六、场景四：用冷应用冷脏页替代热应用干净冷页

### 6.1 目的与容量关系

场景四用于验证用户提出的“回收替代”假设：当冷应用的干净页不足以覆盖目标，而冷应用还有脏冷页时，能否优先处理冷应用脏页，从而保留随后要复用的热应用干净冷页。

受控容量为：

| 页面池 | 容量 |
|---|---:|
| 五个冷应用干净冷页 | 480 MiB |
| 五个冷应用脏冷页 | 400 MiB |
| 三个热应用干净冷页 | 384 MiB |
| 三个热应用脏页 | 192 MiB |
| 回收目标 | 768 MiB |

满足：

```text
cold_clean(480) < target(768) <= cold_clean+cold_dirty(880)
cold_clean(480)+hot_clean(384) >= target(768)
```

因此 Native 可以用“冷干净页 + 热干净页”完成任务；预测方案则具备用“冷干净页 + 冷脏页”替代热干净页的容量条件。

### 6.2 自动化动作

1. 启动相同 8 个 GUI 应用，每个 scope 内启动具有独立 inode 的 `clean.data`、`dirty.data`、`hot.data` fixture。
2. `clean.data` 写入并 fsync 后执行 `MADV_COLD`；`dirty.data` 先 fsync，再逐页改写但不 flush，然后执行 `MADV_COLD`；`hot.data` 只随前台切换访问。
3. 执行训练集对齐序列。最终 VLC 事件前再次 `REDIRTY` 五个预测冷应用，缩短后台 flusher 提前清洗脏页的时间窗。
4. 验证五个冷应用均已通过 `/dev/myfs` 获得 `FILE_DIRTY` 画像，且压力前 clean/dirty/hot 页面驻留和 `memory.stat file_dirty` 达标。
5. 在实验 cgroup 内申请 1536 MiB，形成约 768 MiB 回收目标。
6. 压力结束后用 `mincore()` 分别统计每个 inode 的干净、脏、热页面驻留下降。
7. 依次切回 Firefox、Thunderbird、VLC，逐页读取每个应用完整 128 MiB `clean.data`，随后再 warm 读取一次作为噪声对照。

### 6.3 结果与结论

三轮均值：

| 指标 | Native | LSTM + bin | bin + cold-aggressive |
|---|---:|---:|---:|
| 热干净页保留率 | 25.39% | **100%** | **100%** |
| 冷来源占比 | 49.75% | **95.43%** | 93.17% |
| 冷脏区域回收 | 2.56 MiB | **320.00 MiB** | 162.25 MiB |
| 热干净区域回收 | 286.52 MiB | **0** | **0** |
| 首次 file refault | 95,061 | **0** | **0** |
| 首次 major fault | 3 | **0** | **0** |
| 首次复用耗时 | 243.95 ms | **17.75 ms** | 22.38 ms |
| 压力 PSI full | 60.70 ms | 63.05 ms | **629.44 ms** |
| direct pgscan | 194,966 | 193,421 | **269,687** |
| pswpout | 0 | 0 | **74,859 页** |

场景四包含两个结论：

1. **LSTM + bin 的替代保护通过。**即使没有 cold-aggressive，原生 reclaim/flusher 协同仍允许 bin-only 从排在前面的冷 cgroup 回收足够页面，完整保护热干净页，并消除后续热页复用 refault。
2. **cold-aggressive 在本条件下不合格。**它虽然真实执行，但进入该路径前 `sc->may_writepage` 已经为 1，`writepage_promotions=0`；额外 priority、swappiness 和深扫描没有新增保护对象，只增加了扫描、匿名换出、major fault 和 PSI。

因此场景四不是说明脏页永远不能被回收，而是证明：当 bin-only 已经能够完成替代时，无条件 cold-aggressive 没有边际收益。

## 七、场景五：受限原生写回下的 cold-aggressive 增量收益

### 7.1 目的与场景四的区别

场景五专门构造场景四没有满足的条件：

- 冷应用干净页明显不足；
- 冷应用脏页足够补齐回收缺口；
- 热应用有大量随后必须复用的干净冷页；
- reclaim 初始处于 `may_writepage=0`；
- 压力窗口足够短，bin-only 不能总是等待后台写回追平。

容量为：

| 页面池 | 容量 |
|---|---:|
| 五个冷应用干净冷页 | 120 MiB |
| 五个冷应用脏冷页 | 640 MiB |
| 三个热应用干净冷页 | 576 MiB |
| 回收目标 | 512 MiB |

满足：

```text
cold_clean(120) < target(512) <= cold_clean+cold_dirty(760)
cold_clean(120)+hot_clean(576) >= target(512)
```

### 7.2 自动化动作

1. 启动 8 个 GUI 应用及对应 clean/dirty/hot fixture，并执行相同训练序列。
2. 完成 LSTM、绑定和五个冷应用 `FILE_DIRTY` 画像门禁后，再次立即 `REDIRTY` 五个冷应用；该 socket 命令不产生新的前台事件，不改变 LSTM 历史。
3. 每轮实验临时把 `vm.laptop_mode` 设置为 600，使本轮 direct reclaim 初始进入 `may_writepage=0`，并在压力前再次采集门禁证据。
4. 压力前确认三类文件至少 95% 驻留，且冷 cgroup 的 `file_dirty` 达到配置要求。
5. 把父实验 cgroup 的 `memory.max` 设置为当前使用量加 512 MiB。
6. 在同一子树内以 64 MiB 连续申请 1024 MiB，形成约 512 MiB 定向回收缺口；`memory.swap.max=0`，压力后不等待。
7. 立刻使用 `mincore()` 采集各 inode 驻留变化。
8. 依次精确重读 Firefox、Thunderbird、VLC 各 192 MiB `clean.data`，随后进行 warm 重读。
9. cold-aggressive 组必须观察到 `writepage_promotions>0`，否则本轮直接判为 `INVALID`。
10. 无论正常结束还是异常退出，runner 都在嵌套 `finally` 中恢复原始 `vm.laptop_mode`。

### 7.3 结果与结论

bin-only 与 cold-aggressive 各 3 轮，6 轮全部 `VALID`。三轮均值：

| 指标 | bin-only | cold-aggressive | 变化 |
|---|---:|---:|---:|
| 冷脏区域回收 | 323.01 MiB | **359.63 MiB** | +11.33% |
| 热干净页回收 | 25.49 MiB | **0** | -100% |
| 热干净页保留率 | 95.57% | **100%** | +4.43 个百分点 |
| 首次 file refault | 13,971 | **0** | -100% |
| 首次 major fault | 1 | **0** | -100% |
| 首次文件读取 | 54.58 MiB | **0** | -100% |
| 首次复用耗时 | 53.27 ms | **20.91 ms** | -60.75% |
| 压力 PSI full | 55.79 ms | **31.87 ms** | -42.88% |
| pgscan | 130,066 | **142,109** | +9.26% |

cold-aggressive 三轮分别产生 1,742、2,125、1,969 次 `writepage_promotions`，正式轮次 workload profile miss 均为 0。这证明增强机制不只是开关打开，而是确实在初始写回受限状态下发生了动作。

结论：场景五通过。cold-aggressive 在严格触发条件下具有增量收益，但代价是扫描量增加；其中一轮 bin-only 已由原生 flusher 及时追平，增强策略没有额外 refault 收益且复用慢 6.81 ms，再次说明该策略应由缺口和成本门控，而不能常开。

## 八、五个场景的统一判定

| 场景 | 核心问题 | 当前判定 | 能说明什么 |
|---|---|---|---|
| 一：冷应用退出 | 后续不用的应用是否优先回收 | **通过** | LSTM bin 能稳定集中回收预测冷应用 |
| 二：即将复用保护 | 应用级预测热能否保护页面级冷页 | **通过** | file refault、major fault、复用 direct scan 和延迟均下降 |
| 三：来源分布 | 多应用下回收是否主要来自预测冷应用 | **通过** | 相同总回收量中约 99% 来自预测冷应用 |
| 四：冷脏替代热干净 | 冷脏页能否替代热干净页；增强是否总有收益 | **bin-only 通过，cold-aggressive 不通过** | bin 已能完成保护；无条件增强会增加 swap、扫描和 PSI |
| 五：写回受限门禁 | 真正需要提前处理冷脏页时增强是否有收益 | **有条件通过** | promotion 生效并消除热页 refault，但增加 pgscan |

这五个场景共同支持如下策略：

```text
默认：LSTM + reclaim-bin

仅当：
  required_reclaim - predicted_cold_clean_reclaimable > 0
  且 predicted_cold_dirty 足以覆盖该缺口
  且预测热页面存在较高复用成本
  且原始写回/回收进度不足
时：短时开启 cold-aggressive

若 pgscan、PSI、写入、swap 或 major fault 超过预算：
  立即退回 bin-only
```

## 九、指标口径

| 指标 | 本报告中的含义 |
|---|---|
| `workingset_refault_file` | 文件页离开工作集后又被访问，是判断热文件页误回收的核心指标 |
| `workingset_refault_anon` / `pswpin` | 匿名页被回收/换出后再次访问，反映匿名工作集恢复成本 |
| `pgmajfault` | 需要等待 I/O 的缺页；比单纯 `pgfault` 更接近用户可见停顿 |
| `pgfault` | 包含页表首次建立等大量 minor fault，不能单独判断方案收益 |
| `pgscan_direct` | 前台或压力进程为满足自身分配而同步扫描的页面数 |
| PSI `some/full` | 进程因内存压力停顿的累计时间；full 表示所有非空闲任务同时受阻 |
| `mincore()` 驻留下降 | 精确判断某个受控 inode 的页面是否离开驻留集，用于回收来源归因 |
| `writepage_promotions` | cold-aggressive 将原本不允许 writepage 的扫描提升到允许处理写回路径的动作证据 |

## 十、当前边界与下一阶段

1. 现有结果证明的是“预测正确时机制可以获得收益”，并不证明当前 LSTM 对任意真实 PC 使用序列都足够准确。
2. 场景一至三使用 r7，场景四、五使用 r11；需要最终发布前在同一个定版内核上重跑完整五场景回归。
3. 当前以三轮配对为主，适合机制判断；正式交付建议扩展到至少 5–10 个有效配对轮次，并报告中位数、分位数和置信区间。
4. 场景主要突出文件页保护。匿名热页的 swap-in/refault 收益仍应增加专门复用实验。
5. 下一阶段真实 PC 验证应保留同一事件序列，持续采集前台切换延迟、前台 cgroup PSI、file/anon refault、swap-in、major fault和模型 Top-k 命中率。

## 十一、报告、配置与原始结果索引

- 场景一至三正式报告：[native-vs-r7-bin-lstm-trained-sequence-20260828-lzx.md](/home/lzx/Desktop/PARP/test_reports/native-vs-r7-bin-lstm-trained-sequence-20260828-lzx.md)
- 场景一至三配置：[parp-trained-sequence-config-lzx.json](/home/lzx/Desktop/PARP/test/test/parp-trained-sequence-config-lzx.json)
- 场景一至三 runner：[parp-trained-sequence-experiment-lzx.py](/home/lzx/Desktop/PARP/test/test/parp-trained-sequence-experiment-lzx.py)
- 场景四正式报告：[native-vs-r11-cold-dirty-preserve-hot-clean-20260830-lzx.md](/home/lzx/Desktop/PARP/test_reports/native-vs-r11-cold-dirty-preserve-hot-clean-20260830-lzx.md)
- 场景四配置：[parp-cold-dirty-preserve-config-lzx.json](/home/lzx/Desktop/PARP/test/test/parp-cold-dirty-preserve-config-lzx.json)
- 场景五正式报告：[r11-fifth-cold-writeback-gate-bin-vs-aggressive-20260830-lzx.md](/home/lzx/Desktop/PARP/test_reports/r11-fifth-cold-writeback-gate-bin-vs-aggressive-20260830-lzx.md)
- 场景五配置：[parp-cold-writeback-gate-config-lzx.json](/home/lzx/Desktop/PARP/test/test/parp-cold-writeback-gate-config-lzx.json)
- 场景四、五共同 runner：[parp-real-pc-experiment-lzx.py](/home/lzx/Desktop/PARP/test/test/parp-real-pc-experiment-lzx.py)
- 场景四、五页面 fixture：[reclaim-substitution-fixture-lzx.py](/home/lzx/Desktop/PARP/test/test/reclaim-substitution-fixture-lzx.py)
