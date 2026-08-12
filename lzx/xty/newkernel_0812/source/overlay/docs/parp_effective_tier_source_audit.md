# PARP Effective Tier：Linux 6.17.13 MGLRU 源码语义审计

## 1. 审计基准与结论

本文只以当前工作树中的实际源码为依据，不使用旧 PARP 报告推断 MGLRU 语义：

- 工作树：linux-6.17.13-parp-effective-tier
- 分支：feat/parp-effective-tier
- 审计快照：a5ad006e8b66332a12e13f5e2dc7324bd6111d4a
- 内核版本：Makefile 中 VERSION=6、PATCHLEVEL=17、SUBLEVEL=13
- 下文行号均指向上述快照；后续插入代码会使行号漂移，实现时须再按函数和条件复核。

源码审计结论为 **PARP_EFFECTIVE_TIER_AUDIT_COMPLETE**。

可实验覆盖的原生决策只有 <code>sort_folio()</code> 中普通的
<code>tier &gt; tier_idx</code> 边界保护，而不是
<code>sort_folio()</code> 的整体返回值。必须单独识别并保留同一
<code>if</code> 中的饱和 lazy-promotion 条件，也必须保留其前后的所有非
tier 门控。

关键事实：

1. <code>native_tier = PG_workingset ? 3 :
   order_base_2(folio_lru_refs(folio))</code>。模型不得修改
   <code>PG_referenced</code>、<code>LRU_REFS_MASK</code> 或
   <code>PG_workingset</code>。
2. <code>native_tier_idx</code> 不是常量；它由每个 lruvec、每个
   anon/file 类型的
   <code>refaulted/(evicted+protected)</code> 动态反馈选出。
3. 普通保护的精确条件是严格大于：<code>tier &gt; tier_idx</code>。
4. <code>refs + workingset == BIT(LRU_REFS_WIDTH) + 1</code> 是与动态
   tier 比较并列、且不受 <code>tier_idx</code> 控制的饱和 lazy
   promotion；第一版模型绝不能取消它。
5. <code>folio_inc_gen()</code> 的实际调用包含 generation 窗口维护、
   zone 不可回收、dirty/writeback 等非访问原因。因此
   **generation move 不等于真实页面访问**。
6. <code>sort_folio()</code> 和拟插入的有效 tier 门控位于
   <code>lruvec-&gt;lru_lock</code> 持有且本地 IRQ 禁用的区间内；
   评分必须有常数上界、无分配、无睡眠。

## 2. 逐项源码语义表

|项目|文件|函数|行号|语义|是否允许模型覆盖|
|---|---|---|---:|---|---|
|tier 数量与存储|include/linux/mmzone.h|宏与 struct lru_gen_folio|386–434, 475–494|<code>MAX_NR_TIERS=4</code>。tier 使用 PG_referenced、LRU_REFS_MASK、PG_workingset 表示；反馈统计按 anon/file 和 tier 分组。|否；不得改原生字段或数组语义。|
|refs 宽度|kernel/bounds.c；include/linux/page-flags-layout.h|main；宏|25–30；111–118|<code>__LRU_REFS_WIDTH=MAX_NR_TIERS-2</code>，实际 LRU_REFS_WIDTH 还受 folio flags 剩余位数限制。|否。|
|native refs 读取|include/linux/mm_inline.h|folio_lru_refs|144–155|未置 PG_referenced 时返回 0；否则返回 LRU_REFS_MASK 数值加 1。|否；模型只读快照。|
|native tier 计算|include/linux/mm_inline.h|lru_tier_from_refs|136–142|<code>workingset ? MAX_NR_TIERS-1 : order_base_2(refs)</code>。完整 2-bit refs 配置下，refs 0/1→tier 0，2→1，3/4→2；PG_workingset→3。|否；effective tier 不得写回。|
|FD/mark-accessed 证据累加|mm/swap.c|lru_gen_inc_refs；folio_mark_accessed|389–411；455–462|MGLRU 开启时 folio_mark_accessed 原子累加 refs；refs 饱和后置 PG_workingset。该函数未按 anon/file 禁止调用，不能把“FD 是主要设计来源”误写成“只有 file folio 有 tier”。|否；这是原生历史证据。|
|PTE/rmap 证据与 tier|include/linux/mmzone.h；mm/vmscan.c|注释；folio_update_gen；lru_gen_set_refs|413–432；3258–3280；881–898|PTE 重复访问主要推动 generation，并可置 PG_workingset；所以 tier 3 不一定只来自 FD refs 饱和。|否。|
|PID 反馈定义|mm/vmscan.c|控制器注释|3159–3180|P 项是当前被回收代的 refaulted/(evicted+protected)；I 项是以 1/2 平滑的历史代 EMA；无 D 项。SP 是同类型 tier 0。|否；模型分数不能替换原生反馈。|
|反馈读取|mm/vmscan.c|read_ctrl_pos|3188–3205|指定 type/tier 的分子是 avg_refaulted+current refaulted，分母是 avg_total+current protected+current evicted。tier==MAX_NR_TIERS 时才汇总所有 tier，用于 anon/file 选择。|否。|
|反馈比较|mm/vmscan.c|positive_ctrl_err|3243–3252|PV refault 样本少于 MIN_LRU_BATCH 时视为不需保护；否则用交叉相乘和 gain 比较 SP/PV 率，无浮点。|否。|
|native tier_idx 获取|mm/vmscan.c|get_tier_idx|4670–4688|tier 0/gain 2 作 SP，依次以 tier 1…3/gain 3 作 PV；首个不再满足 positive_ctrl_err 的 tier 开始被保护，返回它的前一 tier。范围 0…3；3 表示普通 tier 保护全关。|否；只能比较，不得改写。|
|anon/file 扫描选择|mm/vmscan.c|get_type_to_scan；isolate_folios|4690–4729|极端 swappiness 直接选 file 或 anon；其他情况比较两类全 tier 反馈。选中 type 后取该 type 自己的 tier_idx。|否。|
|sort_folio 预读状态|mm/vmscan.c|sort_folio|4464–4476|先读 gen/type/zone/pages/refs/workingset/native tier，type 来自 folio_is_file_lru。|仅允许读取一致快照。|
|1. unevictable|mm/vmscan.c|sort_folio|4480–4488|从 MGLRU 删除，转入 unevictable LRU，计数并返回 true。|绝对不允许。|
|2. 已被其他路径 promotion|mm/vmscan.c|sort_folio|4490–4494|若 folio 已不在该 type 最老代，只整理到实际 gen 列表并返回 true；也是并发正确性检查。|绝对不允许。|
|3a. 普通 native tier 保护|mm/vmscan.c|sort_folio|4496–4509|<code>tier &gt; tier_idx</code> 时推进一代、移动列表、按 native type/tier 增加 protected，返回 true。|**唯一可实验覆盖的原生门控**，且受第 6 节安全条件限制。|
|3b. 饱和 workingset/lazy promotion|mm/vmscan.c|sort_folio|4496–4507|<code>refs+workingset == BIT(LRU_REFS_WIDTH)+1</code> 时无条件推进一代；明确不计入 protected。PG_workingset 单独为真只使 tier=3；“特殊 lazy”是上述饱和组合，不是所有 tier-3 folio。|第一版绝对不允许取消。|
|4. zone 不可回收|mm/vmscan.c|sort_folio|4511–4516|zone 大于 reclaim_idx 时推进一代、移到目标列表尾并返回 true。|绝对不允许。|
|5. dirty/writeback 状态及 file 计数|mm/vmscan.c|sort_folio|4518–4524|读取 dirty/writeback；仅 file+dirty 增加 file_taken，未 writeback 时增加 unqueued_dirty。|否。|
|6. 等待 writeback/file dirty|mm/vmscan.c|sort_folio|4526–4531|writeback 或 file+dirty 时以 reclaiming=true 推进一代（同时置 PG_reclaim）并返回 true。writeback 对两类都生效。|绝对不允许。|
|7. 原生候选回收|mm/vmscan.c|sort_folio|4533|前述条件全不成立才返回 false。false 只表示进入 isolate 尝试，不表示必然 isolate/reclaim。|可由 predictive upgrade 变为本轮策略保护；不得伪造必然回收结果。|
|isolate 后续门控|mm/vmscan.c|isolate_folio|4536–4566|即使 sort_folio=false，无 __GFP_IO 下的 dirty/anon non-swapcache、获取引用失败或 LRU 竞争都可使 isolate 失败。|绝对不允许；预测降级必须继续经过它。|
|folio_inc_gen 的实际动作|mm/vmscan.c|folio_inc_gen|3283–3311|只把尚在当前最老代的 folio 推进一代，清 LRU_REFS_FLAGS，可选置 PG_reclaim，更新 generation size；列表移动由调用者完成。|不得改变原生语义；policy promotion 可复用一代移动，但须独立计数且不记作访问。|
|代窗口清理中的 promotion|mm/vmscan.c|inc_min_seq|3875–3925|为防 cold/hot inversion，在推进 min_seq 前逐个推进最老代残留 folio；除特殊 lazy 情形外计入 native protected。该路径未证明有新访问。|否。|
|sort_folio 的 lru_lock 范围|mm/vmscan.c|evict_folios→isolate_folios→scan_folios→sort_folio|4751–4760；4709–4729；4569–4667|spin_lock_irq 在 isolate_folios 前获取；扫描、反馈读取、逐 folio 决策、列表/尺寸/保护统计及 try_to_inc_min_seq 都在锁内，随后释放。shrink_folio_list 在锁外。|只允许按规则修正门控；计算须有常数上界且无分配/睡眠/I/O/浮点/用户态交互。|
|回收后 putback 锁域|mm/vmscan.c|evict_folios|4762–4816|shrink_folio_list 在锁外；移动未回收 folio 回 LRU、批量对账及 vmstat 时在 4796–4816 再持锁。|否。|
|native protected 更新|mm/vmscan.c|sort_folio；inc_min_seq|4496–4507；3905–3915|以 folio_nr_pages 为单位、native type/tier 为索引；饱和 lazy promotion 不计入。字段只在 LRU 锁下修改。|否；不得以 effective tier 为索引，也不得把 policy-only promotion 塞入 native protected。|
|native evicted 更新|mm/workingset.c|lru_gen_eviction|232–257|实际 eviction 时按 native refs/workingset 得到 tier，增加对应 evicted 原子计数，并把 min_seq、refs、workingset 编入 shadow。|否。|
|native refaulted 更新|mm/workingset.c|lru_gen_refault|283–324|仅 shadow 仍 recent 且新 folio 属于同一 lruvec 时，以 shadow 中原 eviction refs/workingset 重建 tier 并增加 refaulted；可恢复 workingset 或 refs。|否；future access 不能写入或称为真实 refault。|
|代际反馈衰减/清零|mm/vmscan.c|reset_ctrl_pos|3207–3241|min_seq 推进时，把当前 refaulted 与 evicted+protected 分别并入 EMA 后除 2，并按 NR_HIST_GENS 策略清理桶。|否。|
|访问证据与代移动分界|mm/vmscan.c|walk_pte_range；lru_gen_look_around；walk_update_folio|3560–3590；4230–4323；3498–3520|PTE/PMD young 位成功清除后才有页表访问证据，然后可调用 folio_update_gen。真实证据是 young/reference 观测，不是随后的 size/list 变化。|不可覆盖证据定义；模型只读真实事件派生历史。|

## 3. native tier 与 tier_idx 的精确关系

### 3.1 native tier

对 <code>sort_folio()</code> 当前 folio：

    refs        = folio_lru_refs(folio)
    workingset  = folio_test_workingset(folio)
    native_tier = workingset ? 3 : order_base_2(refs)

PG_workingset 优先于 refs 计算，所以 native tier 不能解释成单纯的 FD
访问计数。PTE 重复访问、FD refs 饱和和 recent refault 恢复都可影响
PG_workingset。

### 3.2 tier_idx

<code>get_tier_idx(lruvec, type)</code> 对 anon/file 分开执行。返回值是本轮
普通 tier 门控“不保护的最高 tier”：

|tier_idx|原生普通保护的 tier|
|---:|---|
|0|1, 2, 3|
|1|2, 3|
|2|3|
|3|无；但饱和 lazy promotion 仍生效|

边界随 refault/eviction/protection 统计变化，不能在批次之外缓存为全局常量。
当 PV 的 recent-refault 样本少于 MIN_LRU_BATCH 时，控制器倾向关闭该 tier
保护，这也是反馈不足时 tier_idx 可到 3 的原因。

## 4. sort_folio 的真实控制流

    读取 gen/type/zone/refs/workingset/native_tier
      ├─ unevictable?                         -> 转 unevictable LRU, true
      ├─ 已不在最老代?                       -> 整理到实际 gen, true
      ├─ native_tier > tier_idx?             -> 普通 tier 保护, +1 gen, true
      │    或 refs+workingset 达饱和?         -> 特殊 lazy promotion, +1 gen, true
      ├─ zone > reclaim_idx?                 -> 不可回收, +1 gen, true
      ├─ writeback 或 file+dirty?            -> 等待回写, +1 gen, true
      └─ false
           └─ isolate_folio()
                ├─ swap/GFP 限制?            -> isolate 失败
                ├─ folio ref/LRU 竞争?       -> isolate 失败
                └─ 成功脱链                  -> shrink_folio_list()

因此：

- predictive downgrade 只能跳过普通 tier-protection 子分支，不能直接
  isolate；zone、dirty/writeback 和 isolate 自身条件必须继续执行。
- predictive upgrade 不能改写 native tier；可以独立 policy promotion 的
  身份推进一代。
- <code>sort_folio()==true</code> 包含完全不同的语义，不能统称为“tier
  保护”或“真实访问”。

## 5. 不可覆盖与可实验覆盖的边界

### 5.1 绝对不可覆盖

第一版必须无条件保留：

- unevictable 处理；
- folio 已被其他路径推广、已不在最老代的 race 处理；
- <code>refs + workingset == BIT(LRU_REFS_WIDTH)+1</code> 的饱和 lazy
  promotion；
- <code>zone &gt; reclaim_idx</code> 的不可回收处理；
- writeback 和 file-dirty 保护；
- <code>isolate_folio()</code> 的 GFP/swap、引用及 LRU 竞争门控；
- 第一版中 <code>native_tier &gt;= tier_idx + 2</code> 的强普通原生保护；
- native refs/workingset/tier 字段与原生统计的索引含义。

不要把“任何 PG_workingset”与“源码中的特殊饱和 lazy 条件”混为一谈。
源码中的后者是精确的
<code>refs + workingset == BIT(LRU_REFS_WIDTH)+1</code>。若实验再规定所有
PG_workingset 都不可降级，那是更保守的 PARP 策略，不是该原生特殊分支的
字面语义。

### 5.2 可实验覆盖

只允许在普通 tier 门控中执行两种修正：

- 预测升级：<code>native_tier &lt;= tier_idx</code> 但
  <code>effective_tier_q8 &gt; tier_idx*256</code>，将原生候选回收改为
  本轮 policy protection，最多推进一代；不置访问位、不改 native tier、
  不记 native protected。
- 预测降级：仅在 <code>native_tier == tier_idx+1</code>、特殊 lazy
  条件为假、模型/状态/预算全部有效且冷证据满足时，允许跳过本次普通 tier
  promotion。最大降低 -1 tier；不移动到更老 generation，并继续执行
  sort_folio 后续门控。

为了保持原生统计的字面含义，policy-only promotion 必须另设计数，不能写入
<code>lrugen-&gt;protected[hist][type][effective_tier]</code>。预测降级
真正跳过原生保护时，也不应将该 folio 记成“已被 native protected”；若需
保留反事实，应使用独立 native_would_protect 统计。这样 APPLY 下的后续
原生控制器看到实际策略结果，但 protected 的含义不被污染。

## 6. effective tier 等价性与插入要求

主门控应保持严格大于：

    native_tier_q8    = native_tier * 256
    effective_tier_q8 = clamp(native_tier_q8 + predictive_delta_q8,
                              0, (MAX_NR_TIERS - 1) * 256)

    native_protect    = native_tier > native_tier_idx
    effective_protect = effective_tier_q8 > native_tier_idx * 256

模型、页面状态、版本、预算或压力状态任一无效时，
<code>predictive_delta_q8</code> 必须为 0。native tier 是整数，因此
delta=0 时定点缩放保持严格大于比较完全等价：

    effective_protect == native_protect

实现顺序：

1. 保留源码现有的入口快照顺序：先读
   gen/type/zone/delta/refs/workingset/native tier；不要因重构改变并发
   <code>folio-&gt;flags</code> 快照时点。
2. 按原顺序完成 unevictable 与已推广 race 检查；只有通过这些检查后才可
   进入预测路径。
3. 使用入口快照单独识别饱和 lazy 条件；该条件优先并直接执行 Native。
4. 使用调用者在本次 type 扫描前由 <code>get_tier_idx()</code> 算出的
   tier_idx。OFF 在读 page_ext/模型前走 Native；其他模式才读取有效状态并执行有界
   评分。
5. SHADOW 只记录四象限，实际分支始终使用 native_protect。
6. APPLY 按上述安全条件选择普通 tier 分支。
7. 如未在此保护，继续原生 zone、dirty/writeback 与 isolate 路径。

## 7. generation move 不是真实访问

### 7.1 folio_inc_gen 的全部实际调用

|调用点|原因|是否证明此刻有真实访问|允许更新的 PARP 状态|
|---|---|---|---|
|mm/vmscan.c:3905，inc_min_seq|最老代残留页不能阻塞 generation 窗口，避免 cold/hot inversion|否|generation enter/current generation、native promotion 计数|
|mm/vmscan.c:4498，sort_folio|普通 tier 保护或饱和 lazy promotion|否；refs 是此前累积证据，移动本身不是新访问|generation enter/current generation、native promotion 计数|
|mm/vmscan.c:4513，sort_folio|zone 超过 reclaim_idx|否|generation enter/current generation、other move 计数|
|mm/vmscan.c:4528，sort_folio|writeback 或 file dirty|否|generation enter/current generation、other move 计数|

此外，<code>lru_gen_update_size()</code> 还在添加、删除、推广和批量尺寸对账时
调用（include/linux/mm_inline.h:174–218, 253–300）。因此挂在
<code>lru_gen_update_size()</code> 或通用 list move 上的 access hook 会把
策略、维护和 putback 误标成访问。

### 7.2 可作为真实在线访问证据的源码边界

|PARP 事件类型|应绑定的原生证据|可否更新 last_access 等访问历史|
|---|---|---|
|PTE/PMD young|成功的 ptep_clear_young_notify / pmdp_clear_young_notify，例如 mm/vmscan.c:3573–3590, 4253–4315|可，但须与成功清 young 的 folio 精确对应|
|rmap reference|folio_referenced 返回正的 referenced PTE 后进入 lru_gen_set_refs，mm/vmscan.c:906–935|可；证据是 referenced PTE，不是后续 activation/generation move|
|folio_mark_accessed|MGLRU 路径实际进入 lru_gen_inc_refs，mm/swap.c:455–462|可；它表示内核调用者报告的访问，不是所有 CPU 访存的完整日志|
|FD reference|能证明实际 buffered/FD 访问并调用 folio_mark_accessed 的路径|可；不能仅因读取 refs 或执行 tier 保护而更新|
|native tier promotion|sort_folio 的普通保护分支|否|
|native generation maintenance|inc_min_seq、aging 推进、列表整理|否|
|PARP policy promotion|预测升级造成的 folio_inc_gen/list move|否|
|putback/list move|未回收 folio 重新加入 LRU 或只整理列表|否|

只有前四类经精确绑定的真实证据可更新 last_access、
previous_access_interval、access_ema、reuse_interval_ema、access_count 和
连续未访问状态。后四类只能更新 generation 进入时间、当前 generation 及
native/policy/other-move 计数。

必须阻断如下自强化回路：

    PARP policy promotion
      -X-> 不得写 last_access
      -X-> 不得让下一次评分因“伪访问”继续变热

## 8. anon/file 差异

|维度|Anon|File|
|---|---|---|
|native tier 公式|相同|相同|
|min_seq / 当前最老代|min_seq[LRU_GEN_ANON]|min_seq[LRU_GEN_FILE]|
|tier 反馈和 tier_idx|使用 anon 自己各 tier 的 refaulted/evicted/protected|使用 file 自己各 tier 的 refaulted/evicted/protected|
|扫描类型选择|受 swappiness 和两类累计反馈比较影响|同左|
|典型访问证据|主要是 PTE/PMD young 与 rmap reference；但 lru_gen_inc_refs 本身不禁止 anon|可同时来自页表 mapping 与 buffered/FD folio_mark_accessed|
|dirty 处理|dirty 本身不触发 file&&dirty 分支；writeback 仍触发保留|dirty 即触发等待回写分支，并更新 file-specific reclaim 计数|
|isolate 的 swap 约束|无 __GFP_IO 且非 swapcache 时 isolate 失败|无 anon-non-swapcache 这一条；dirty 约束仍可生效|

effective-tier 决策函数可共享一套公式，但必须传递当前 type，使用该 type 的
tier_idx、min_seq 和统计。不得用 file 反馈边界修正 anon folio，反之亦然。

## 9. 实现阶段源码约束清单

- 插入点在 sort_folio 内部普通 tier 门控，而不是其返回 false 之后。
- 将当前合并的 <code>tier &gt; tier_idx || saturated_lazy</code> 拆成
  语义独立的特殊 Native 决策和普通 tier 决策，但 Native 模式的顺序、
  列表和统计效果必须不变。
- OFF 必须在读取 page_ext/模型前快速走 Native；失效时 delta=0，并测试
  严格大于比较完全等价。
- SHADOW 不得改变 generation、list order、native protected 或 isolate
  结果。
- 预测降级只是“跳过本轮普通 native tier promotion”，不是降低 native
  tier，也不是移向更老 generation。
- 预测升级只是一代 policy promotion，不是真实页面访问，不得更新
  last_access。
- native protected、evicted、refaulted 始终以 native type/tier 为索引；
  四象限、policy promotion/downgrade 和反事实必须使用独立 PARP 统计。
- 模型在 lru_lock+IRQ-disabled 区域内执行，须分别为逐 folio 评分时延与整个
  锁持有时延设置门禁。
