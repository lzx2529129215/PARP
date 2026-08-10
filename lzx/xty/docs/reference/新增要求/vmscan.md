逻辑很集中但覆盖面广，主要包括：

- LRU 页面扫描与回收：从活跃/非活跃的匿名页、文件页队列中挑选较少使用的页。
- 脏页处理：脏文件页不能直接丢弃，需经 writeback 写回存储后才可回收。
- 匿名页换出：匿名页若无法直接释放，可写入 swap 分区/交换文件。
- 页迁移/降级：在分层内存机器中，冷页可从更快内存迁移到较慢内存，而不立即丢弃。
- MemCG 支持：内存 cgroup 超出自身限制时，只针对该 cgroup 执行回收，并处理 `memory.low` 等保护策略。
- NUMA 支持：按内存节点（node）和 zone 做回收，避免不必要地影响其他节点。
- 后台与直接回收：
    - `kswapd()`：每个 NUMA node 的后台回收内核线程；被 `wakeup_kswapd()` 唤醒后提前回收，尽量避免业务线程卡住。
    - `try_to_free_pages()`：分配内存失败时，由当前申请内存的线程直接执行回收，压力大时会增加延迟。
- 多代 LRU（MGLRU）：此版本默认包含较大篇幅的多代 LRU 逻辑。它按“页面使用的新旧代际”而非仅传统 active/inactive LRU 来判断冷热，通常能更准确地回收冷页，减少错误回收与重新读入。

可以将它的主流程概括为：
```
内存分配发现空闲页不足
          │
          ├─ 唤醒后台 kswapd
          │
          └─ 必要时直接回收 try_to_free_pages()
                         │
                         ▼
              按 node / zone / memcg 选择扫描范围
                         │
                         ▼
              从 LRU / 多代 LRU 中隔离候选 folio
                         │
            ┌────────────┼─────────────┐
            ▼            ▼             ▼
         干净页        脏文件页       匿名页
         直接释放      写回后释放     swap 后释放
                         │
                         ▼
                    得到空闲物理页
```

## 第一核心结构：`struct scan_control`结构体：
```
它描述“一次回收任务”的上下文，重点字段：
- `nr_to_reclaim`：目标回收多少页
- `gfp_mask`：当前分配允许做什么，例如能否进入文件系统、能否 I/O
- `order`：申请的内存阶数
- `priority`：回收激进程度
- `reclaim_idx`：最多从哪个 zone 回收
- `may_writepage`：是否允许写回脏页
- `may_unmap`：是否允许解除页表映射
- `may_swap`：是否允许交换匿名页
- `nr_scanned`：扫描页数
- `nr_reclaimed`：真正回收页数
```


## 第二核心函数： `shrink_folio_list`：
```
这是一个 folio 到底能不能被释放”的核心。
核心流程：
	取出 folio
	  ↓
	尝试加锁
	  ↓
	是否可驱逐？
	  ↓
	是否被映射？
	  ↓
	是否 dirty / writeback？
	  ↓
	是否最近被访问？
	  ↓
	匿名页：分配 swap
	文件页：准备写回
	  ↓
	解除页表映射
	  ↓
	检查 DMA pin
	  ↓
	删除 page cache / swap cache
	  ↓
	释放 folio
```

```
几个重要失败结果：

- 被引用：重新放回 active LRU
- 正在 writeback：等待或延后处理
- dirty 但当前上下文不能写回：放回 LRU
- 仍然 mapped：不能释放
- 被 DMA pin：不能释放
- 无法删除 mapping：放回 LRU

因此，回收并不是“扫描到就 free”，而是一个严格的资格检查过程。
```

## 第三核心函数: `shrink_inactive_list`:

它连接 LRU 和 `shrink_folio_list()`：
```
inactive LRU
  ↓
isolate_lru_folios()
  ↓
临时链表
  ↓
shrink_folio_list()
  ↓
move_folios_to_lru()
```


**`isolate_lru_folios()`：**
先从 LRU 链表摘出来，释放 `lru_lock`，然后在没有持有 LRU 锁的情况下做较慢的操作，例如：
- 页表反向映射
- 写回
- swap
- 文件系统操作
- 解除映射

## MGLRU 路径
这个文件还支持 `Multi-Generational LRU`
主要入口：
- `lru_gen_age_node()`：给页“老化”
- `scan_folios()`：扫描候选 folio
- `evict_folios()`：隔离并回收
- `try_to_shrink_lruvec()`：回收一个 lruvec


**`scan_folios()`**：

## 节点级回收调用链

重点调用链如下：
```
try_to_free_pages()
  → do_try_to_free_pages()
    → shrink_zones()
      → shrink_node()
        → shrink_node_memcgs()
          → shrink_lruvec()
            ├─ 传统 LRU：get_scan_count()
            └─ MGLRU：try_to_shrink_lruvec()
                → evict_folios()
                  → scan_folios()
                  → shrink_folio_list()
```


## kswapd 主循环
### `wakeup_kswapd`
- 当页面分配时会检查水位线，当发现水位线高于low时会调用该函数唤醒kswapd进行内存回收
- `wakeup_kswapd`会依次执行如下逻辑判断是否要开启kswapd回收：
	1. 通过`waitqueue_active()`先判断当前是否已经被唤醒，如果已经被唤醒则不重复唤醒
	2. 通过`pgdat->kswapd_failures >= MAX_RECLAIM_RETRIES`判断是否回收已多次失败，如果多次失败则不唤醒；
	3. 通过`pgdat_balanced`判断目标node是否已满足本次order和目标zone所需水位线，满足则不唤醒
		- `order`：请求连续页的阶数，例如 `order=4` 代表需要 16 个连续页；
	4. 通过`!pgdat_watermark_boosted(...)`判断有没有因为之前的规整失败等情况额外提高水位要求
- 如果满足了1，但是2，3，4有要求不满足，系统就会通过
	- `gfp_flags & __GFP_DIRECT_RECLAIM`
- 判断目前是否开启了`direct reclaim`(直接回收)，如果当前请求不允许直接回收/没有开启直接回收，则会唤醒 `kcompactd` 去进行内存调整，尝试把零散的空闲页合并为一个连续大块。
- 如果上述要求都满足，则会调用`wake_up_interruptible(&pgdat->kswapd_wait)`执行`kswapd`的唤醒
	- `wake_up_interruptible(&pgdat->kswapd_wait)`不会直接调用某个“回收函数”；它只是把睡眠中的 `kswapd` 内核线程设为可运行。
		- `wake_up_interruptible(&pgdat->kswapd_wait)`会把把等待队列中的 `kswapd` 任务状态改为可运行（`TASK_RUNNING`），放回调度器的运行队列。
		- 随后调度器再次运行该线程时，`kswapd` 会从等待位置返回，继续执行它的主循环
	- 对应的等待代码在 `vmscan.c` 文件中的 `kswapd_try_to_sleep()`函数中。

它的行为：
```
睡眠
  ↓
wakeup_kswapd()
  ↓
balance_pgdat()
  ↓
检查 zone watermark
  ↓
kswapd_age_node()
  ↓
kswapd_shrink_node()
  ↓
回收失败则提高 priority
  ↓
达到 watermark 后睡眠
```

### ``kswapd_try_to_sleep`： 
决定后台内存回收线程 `kswapd` 是否可以休眠，以及以何种方式休眠
- 流程：

```
kswapd 完成一轮页面回收
  ↓
检查目标 zone 是否恢复到所需水位线
  ├─ 未恢复：不睡，返回主循环继续回收
  └─ 已恢复：
       ↓
     唤醒 kcompactd，尝试解决高阶分配的内存碎片
       ↓
     先短暂休眠 HZ/10（约 0.1 秒）
       ├─ 被新的分配请求提前唤醒
       │    → 合并新的 order/zone 请求
       │    → 返回 kswapd 主循环，继续回收
       └─ 未被提前唤醒
            → 再检查水位线
            ├─ 仍满足：完全休眠，直到 wakeup_kswapd() 唤醒
            └─ 不满足：继续回收
```
- 该函数主要通过`prepare_to_wait(&pgdat->kswapd_wait, &wait, TASK_INTERRUPTIBLE);`将当前kswapd加入等待队列，并设置为可中断睡眠状态


### `kswapd()`
- 当kswapd线程被调度器唤醒后，会进入这个函数下面的`for ( ; ; ) `中的逻辑
- 具体而言，会从下面这个口子继续往后执行：
```
kswapd_try_sleep:
        /* 无压力时在这里睡眠；收到唤醒请求后从此处继续。 */
        kswapd_try_to_sleep(pgdat, alloc_order, reclaim_order,highest_zoneidx);
```

- 被唤醒后，首先会执行下面两段代码：
	- `alloc_order = READ_ONCE(pgdat->kswapd_order)`
	- `highest_zoneidx = kswapd_highest_zoneidx(pgdat,highest_zoneidx);`
- 分别读取这次回收的 order 和 highest_zone
- 然后会调用`reclaim_order = balance_pgdat(pgdat, alloc_order,highest_zoneidx);`对 `pgdat` 所代表的 `NUMA node` 执行页面回收：扫描满足条件的 zone，直到达到目标水位线或无法继续有效回收。返回实际完成时的 order

### `balance_pgdat` -`vmscan.c`
```
balance_pgdat()
  └─ kswapd_shrink_node()
      └─ shrink_node()
          ├─ MGLRU 已启用 → lru_gen_shrink_node(pgdat, sc)
          └─ MGLRU 未启用 → 传统 active/inactive LRU 回收
```
- **函数总体逻辑：**
	- 从本次请求允许使用的 zone 中回收页面，直到至少一个可用 zone 恢复到目标水位线
		- 请求可用：DMA、Normal
		- 当前状态：DMA 不足，Normal 不足
		- kswapd 回收：从可用范围内的 zone 回收页
		- 停止条件：DMA 或 Normal 任一恢复到目标水位线
- **流程：**
	- 利用 `for_each_managed_zone_pgdat`遍历当前允许的所有zone， 汇总各个 zone 的 `watermark_boost`，`watermark_boost` 可以理解为某个zone临时抬高的水位线。
		- 通常只有当高阶order页面分配失败时，通过内存规整发现：虽然空闲页总数可能还行，但缺少足够大的连续空闲块时，才会设置它
		- 它会要求kswapd多回收一些页面，给规整腾出空间。
		- 在kswapd回收完成之后，会唤醒kcompactd，异步规整连续空闲块
	- 然后进入回收路径，设置 `sc.priority = DEF_PRIORITY;` ，从最大优先级开始进行回收（优先级越小，回收越激进）
```
do
{
	...
	/* 扫描不足或没有回收进展时，降低 priority 以增加下一轮扫描力度。 */
      if (raise_priority || !nr_reclaimed)sc.priority--;
}while(sc.priority >= 1)
```
	- 通过上述循环，每次回收结束后若没有达到条件，就加大回收力度，直到满足条件/达到最大粒度
- **循环：**
	- buffer_head相关逻辑（暂时不用了解）
	- 通过 `balanced = pgdat_balanced(pgdat, sc.order, highest_zoneidx);`判断是否平衡（达到水位线要求），如果还没有达到水位线要求，则先不考虑watermark_boost（额外回收）
	- 设置boost回收逻辑（额外回收逻辑），boost 回收的目的只是缓解短暂压力，必须确保：
		 * 不发起低效的回收上下文 I/O：
		 * 禁止写回和 swap。
		 * 若无法直接回收页面，后续会终止本轮 boost 回收。
	 * 然后会**调用`kswapd_age_node()`**，在回收前做后台老化，给近期使用的页面再次被访问的机会
	 * **`laptop_mode`相关逻辑（暂时不考虑，后续可以引入）**
		 * 主要是用于考虑是否提前开启脏页回收
	 * 然后会先调用 `memcg1_soft_limit_reclaim()` 进行 `soft_limit` 回收[[soft limit]]
		 * 该机制主要用于优先回收超额cgroup，开启MGLRU后直接返回0（不走这套逻辑）
	 * 然后会**调用`kswapd_shrink_node()`进行页面的回收**，并判断是否需要继续扩大扫描力度（priority）
		 * `kswapd_shrink_node()` 做的是“按当前回收力度扫描这个 node 的 LRU 页面”。它会先估算：为了让各 zone 回到 `high watermark`，理论上还需要释放多少页，并据此设定本轮应扫描的页数 `sc.nr_to_reclaim`。
		 * 然后假设扫描的每一页都能成功释放，判断扫描这么多页是否已经足以补齐 high watermark 的缺口？
		 * 如果 `kswapd_shrink_node()` 返回 `true`，表示当前这一轮已经扫描到预期数量（或碰到写回中的页，继续立刻扩大扫描也意义不大）。
	 * 然后会判断是否达到low水位线，达到low水位线后，会唤醒一些限流的任务（kswapd回收时会让部分线程睡眠等待）


### `kswapd_age_node()`

### `kswapd_shrink_node()`

- 在当前 NUMA node 上执行一轮页面回收。回收范围覆盖不高于sc->reclaim_idx 的已管理 zone
- 返回 true 表示本轮已扫描足够数量的页面，或回收未取得进展是因为页面正在写回

流程：
- 调用 `for_each_managed_zone_pgdat`，遍历zone为本轮 `kswapd` 回收设置一个“最低扫描目标”
- **调用 `shrink_node()` 对整个node的LRU页面进行实际回收**
- 判断 `kswapd` 是否已经为规整创造了**足够值得尝试的空闲空间**，如果是则不必继续为了高阶目标过度回收

### `shrink_node()`

会判断是否是MGLRU，如果是，进入`lru_gen_shrink_node(pgdat, sc);`逻辑：
MGLRU对两类回收有不同入口：
```
全局 / node 级回收（root reclaim）
  → shrink_node()
  → lru_gen_shrink_node(pgdat, sc)

单个非根 memcg 的定向回收
  → shrink_lruvec()
  → lru_gen_shrink_lruvec(lruvec, sc)
```
#### lru_gen_shrink_node(pgdat, sc) -- 全局级回收
- MGLRU 的 node 级实际回收入口
- 会进行一些初始化，然后调用 `shrink_many()` 进行memcg级别的回收

#### `shrink_many` -- 进行memcg级别的回收
- NUMA node 的 memcg 不是放在一个总链表里，而是放在：`pgdat->memcg_lru.fifo[gen][bin]`
- 即一个二维FIFO队列
	- 第0维 `gen` 表示 memcg 的 回收轮次
	- 第1维 `bin` 表示该轮次内的随机分桶
```
node
└─ 当前 memcg generation
   ├─ bin 0：部分 memcg
   ├─ bin 1：部分 memcg
   ├─ ...
   └─ bin 7：部分 memcg
```

- **流程**：
	- 首先，shrink_many会选择这次要遍历的gen，然后随机选择一个bin作为开头，依次遍历完剩下的所有bin
	- 然后会调用 `hlist_nulls_for_each_entry_rcu()`遍历当前`node`，当前`memcg generation`，当前 `bin` 内的所有 `lruvec` 调度节点
		- → 释放上一个 memcg 的引用
			→ 从 FIFO 取当前 lruvec
			→ 确认它仍属于当前 generation
			→ 获取当前 memcg 的安全引用
				- `lruvec = container_of(lrugen, struct lruvec, lrugen);`
				- `memcg = lruvec_memcg(lruvec);`
				- 利用上述函数获取当前需要回收的memcg
			→ 退出 RCU 读锁
			→ 回收该 memcg
			→ 回到 RCU 读锁，继续下一个 memcg
	- 然后调用 `shrink_one()` 对获取到的这个 memcg 进行回收
	- 