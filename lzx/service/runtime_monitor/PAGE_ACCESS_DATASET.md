# WPS 每秒文件页访问数据集

## 采集语义

`page-access-window` 是独立的只采集 profile。每个完整窗口固定为 1 秒，一页在该秒内访问一次或多次都只输出一次；数据不声称知道该页在窗口内的具体访问时刻。匿名页不进入注册表。3 秒粒度应在离线阶段对连续三个 1 秒集合取并集，正式采集不会重复生成 3 秒数据。

稳定身份是 `file_catalog_id + page_index`，当次物理身份是 `boot_id + pfn`。已知测试输入的 `file_catalog_id` 由 fixture 逻辑 ID 确定；其内容 SHA-256 在自动化开始前计算。其他有路径文件使用规范化路径哈希；只有无法解析路径的文件退化为 device/inode 身份并显式标记。目录从不保存绝对路径。

## 数据路径

1. Runtime Monitor 继续以 `AppProcessIndex` 和 WPS 固定 App ID 确定目标 PID。`createProcess()`、`exeProcess()`、`destroyProcess()` 后的同步会增量刷新注册表；`mmap/munmap/mremap` 只把对应 PID 标为 dirty，并在窗口边界定向重扫其文件 VMA 与 pagemap。
2. root helper 独占 `/run/parp-page-idle.lock`。启动时只把已注册 WPS 文件页 PFN 写入 `/sys/kernel/mm/page_idle/bitmap`；内核在设置 Idle 时清理相关页表 Accessed 状态。
3. 一秒后只读取这些 PFN 的 Idle 位。位被内核清除表示窗口内至少访问过一次，随后立即验证文件页仍是 LRU、非匿名页并重新 arm 下一窗口。
4. `mm_filemap_get_pages` 和 `mm_filemap_fault` 的直接 WPS 证据写入内核双缓冲 Hash Map，以 `device + inode + page_index` 去重；`mm_filemap_add_to_page_cache` 只维护 PFN 身份与生命周期，不单独判定页被访问。用户态每秒只交换 epoch 并批量导出，不传逐 access 事件。
5. `mm_filemap_add_to_page_cache` 只注册 PFN 和写 `CACHE_ADD` 生命周期，不能单独把页判定为已访问。`FAULT/CACHE_ADD/EVICT/PFN_CHANGE` 写入独立生命周期文件，用于补齐短命页和审计 PFN 迁移/复用。

边界交换先翻转 BPF epoch，再排空关闭的 Map。容量溢出、perf 丢失、Page Idle 读写错误、PFN 无法解析、窗口严重超时都会保留该窗口并标记 `INVALID`。停止时不足一秒的末窗也会保留为 `INCOMPLETE_FINAL_WINDOW`，但试采有效率只统计完整窗口。

归因位含义：

- `DIRECT_WPS`：WPS 线程直接触发 buffered I/O 或文件 fault，置信度最高。
- `WPS_MAPPED_UNIQUE`：Idle 位变化，当前映射计数未超过已知 WPS 映射数。
- `WPS_MAPPED_SHARED`：Idle 位变化且映射计数超过已知 WPS 映射数，保留但不能证明访问者一定是 WPS。
- `UNRESOLVED`：稳定文件页已知但当次 PFN 未解析；该窗口无效。

## 文件格式

每个 session 的采集文件位于 `dataset/`：

- `page_access_windows.bin`：小端版本头，后接长度 + CRC32 的 zlib JSON 帧。窗口记录包含 monotonic/realtime 起止时间、reset/scan 起止及延迟、App ID、质量标志和无损页 range。
- `page_lifecycle.bin`：同样的版本化帧，只含 `FAULT/CACHE_ADD/EVICT/PFN_CHANGE` 及 monotonic/realtime 时间。
- `file_catalog.jsonl`：device/inode、路径 SHA-256、fixture 逻辑 ID、预计算内容哈希、大小和 mtime；不保存路径本身。
- `window_summary.csv`：窗口页数、直接/独占映射/共享、新驻留/回收/未解析页数、reset/scan 延迟、overflow 和有效性。
- `manifest.json`：schema、内核、boot ID、页大小、实验条件、时钟校准、总计和 SHA-256。

自动化步骤在 session 根目录的 `automation_trace.csv`，每行同时包含 realtime 与 monotonic 的事件时间；`OP_DONE/OP_FAILED` 还带对应操作的两个时钟起止时间。

## 安装与单次采集

先安装更新后的 helper：

```bash
cd /home/lzx/Desktop/PARP
bash lzx/service/runtime_monitor/scripts/install_service.sh
```

单独启动 monitor 时必须显式使用固定 profile；它与旧 PageHotsetShadow、DAMON region monitor 和在线训练互斥：

```bash
cd /home/lzx/Desktop/PARP/lzx/service
python3 -u runtime_monitor/monitor.py \
  --output-dir outputs/runtime_monitor/wps_page_access_example \
  --session-id wps_page_access_example \
  --process-event-source connector --require-process-connector \
  --process-cgroup-routing systemd \
  --file-event-source ebpf --require-ebpf-file-events \
  --file-event-profile page-access-window \
  --page-access-target-app WPS \
  --page-access-window-ms 1000 \
  --require-page-idle
```

完整固定协议由以下脚本执行。默认先跑 `0010–0070 × 3` 次试采，验证通过后才继续 `0010–0070 × 20` 次正式采集；不会执行 `drop_caches`：

```bash
cd /home/lzx/Desktop/PARP
test/automation/run_wps_page_access_dataset.sh

# 只试采
WPS_PAGE_ACCESS_PHASE=pilot test/automation/run_wps_page_access_dataset.sh
```

正式 20 次按 10 组组织：奇数次重新启动 WPS，偶数次紧接运行并标为 `warm_cache_repeat`。试采门槛是完整窗口有效率不低于 99%、零 BPF/perf 丢失、零 helper 重启、`reset+scan` p99 不超过 200 ms，且自动化 monotonic 时间位于 capture 时钟区间内。

## 只读验证

正式采集不会自动展开、聚合或训练。只读工具默认验证 manifest、文件哈希、二进制头和每帧 CRC：

```bash
python3 runtime_monitor/scripts/decode_page_access_dataset.py \
  /path/to/session/dataset

# 人工检查时才展开 range；输出到 stdout，不改原数据
python3 runtime_monitor/scripts/decode_page_access_dataset.py \
  /path/to/session/dataset --kind windows --jsonl --expand-ranges

# 崩溃留下 .partial 时，只恢复末尾截断前 CRC 完整的帧
python3 runtime_monitor/scripts/decode_page_access_dataset.py \
  /path/to/session/dataset --recover-truncated
```

真实内核合成验收（需要 root）覆盖 mmap、pread、同秒重复访问、未访问页误报以及 PFN/CRC 路径：

```bash
cd /home/lzx/Desktop/PARP/lzx/service
sudo python3 runtime_monitor/scripts/verify_page_access_synthetic.py
```
