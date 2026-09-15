# 已知未来应用序列与内核 bin

本次实现及实际验收见[实验报告](../../test_reports/user-events-oracle-bin-20260914.md)。历史完整 85 段对照因 WPS 崩溃未通过；后续已接入[WPS修复](../../test_reports/wps-replay-crash-fix-20260914.md)，完整压力对照仍须单独验收。

入口为 `user_events_replay.py --continuous`，原分组回放入口不变。执行计划直接读取对比 Excel 的逐行记录，并对照原始 workbook 校验应用、行号、时间及操作参数。当前计划是 85 段、2230 条应用操作；不包含未执行和桌面/截图/录屏记录。

```bash
cd /home/lzx/Desktop/PARP
python3 test/test/user_events_replay.py --continuous plan \
  --output-dir test/outputs/oracle-new-plan
python3 test/test/user_events_replay.py --continuous preflight \
  --output-dir test/outputs/oracle-new-preflight
python3 test/test/user_events_replay.py --continuous run --no-pressure --max-segments 2 \
  --output-dir test/outputs/oracle-new-smoke
python3 test/test/user_events_replay.py --continuous run \
  --output-dir test/outputs/oracle-new-pair
```

输出目录须不存在。`--comparison` 与 `--xlsx` 可指定对比表和原始文件。完整实验不支持跳过失败段或从任意中点恢复：应用驻留状态不能通过拼接此前结果还原。限定段数只用于无压力检查。

## 顺序和提交

当前段 i 的前台确认后，严格从 i+1 查找各存活后台应用下一次出现位置。展示的回收列表为“无后续访问、较晚、较早”，并列按应用 ID。内核 rank 方向相反：前台 rank=1、score=32767；后台从早到晚 rank=2…N、基础 score=0。

这是 `oracle_next_use_rank_v1` 顺序提示，传输版本为 601，不是概率或 LSTM 输出。适配器复用 myfs v3 结构和真实进程 cgroup 绑定，不经过原概率/WSS 估计路径。禁用 cold-aggressive/workload 概率捷径及附加 effective-tier/WSS 预测，使用当前内核 rank 下限和 8 个 bin；同 bin 内不保证严格先后，前台也不是绝对不可回收。

每次切换必须按段游标递增；弹窗不会推进游标。绑定变化补发，1 秒续租、5 秒 TTL，每批读回 generation、应用项和完整绑定；续租前也核对上一批状态和常驻服务是否仍暂停。动作有 90 秒执行期限；非预期焦点持续超过 750ms、绑定歧义、读回不符、异常应用退出或 OOM 均终止本轮，停止续租并解除压力。

## 运行环境和恢复

一轮使用一个独立 X11 桌面和实验父 slice，各应用服务位于其下。控制器和恢复进程在受压子树外。来源素材分目录，但不会在来源组边界清空后台应用；显式关闭及 WPS/EOG 的既有重开替代仍保留。

应用市场的通用启动使用 `gnome-software --mode=installed --prefer-local`，避免无关的在线应用详情加载阻塞；其他操作继续沿用原映射。

WPS 持续回放使用本次输出目录下的 `wps-runtime/`：复制原版可执行文件和配置，引用已安装的库/资源，仅将副本 OEM 配置中的 `EnableAccount=false` 改为 `true`。对照测试确认，这个安装版本在关闭账号模块时仍启动 qing 自动登录处理并发生段错误。修复不执行账号登录，不修改 `/opt` 安装或日常用户配置；`EnableCloudDocs` 保持原值。源码与副本配置、二进制 SHA-256 写入 `parp-runtime-manifest.json`。

Word/PPT/Excel 分别由 WPS 自带的 `wps/wpp/et` 打开，仍统一归入应用 ID 15 和同一 WPS 子 cgroup。文档重开、源行顺序和游标规则保持原语义；原生组件不依赖融合窗口嵌入完成。文件菜单、启动提示及另存为后的文件名状态已适配。原分组回放默认启动方式保留。

WPS 专项回归（只执行原表中的 WPS 操作段，不代表完整85段应用间切换）：

```bash
python3 test/test/wps_reopen_probe.py --modes components --fixed-runtime \
  --operations --cycles 3 --output-dir test/outputs/wps-regression-new
```

运行前保存控制值和常驻服务状态，暂停 `parp-runtime-monitor.service`，避免全局 myfs 状态被其他发布者覆盖。实验以互斥锁串行执行。独立 guardian 持有锁并通过 pidfd 监测执行器；执行器异常消失时同样解除 memory.max、停止本次单元、等待 5 秒 TTL 过期、恢复控制值及原本活动的常驻服务。恢复结果写入 `recovery-state.json`。

## 压力和验收

完整命令依次执行同一计划的 native 与 oracle 两轮。native 首次满足至少三个后台应用、至少两个不同的有限下次访问位置、父组用量至少 768 MiB 时，将 memory.max 设为当前用量减 192 MiB，60 秒后解除。采用非阻塞 memory.max 写入，压力通过应用后续正常分配触发；不用 memory.reclaim 或额外分配器。swap 上限为 1 GiB。

oracle 复用同一段位置和相同限额；初始内存偏差超过 15% 时判为无效对照。没有满足施压条件则明确 NOT_TRIGGERED，不算回收闭环通过。只有完整回放、真实 pgsteal 增长，并且 oracle 轮压力时间窗内核 context_hits/rank_scores 增长，才报告该层验收通过；性能提升另外判断。内核计数是全局统计，逐应用回收由实验 cgroup 的 memory.stat 另行记录，不推断严格逐应用回收先后。

基线失效后允许独立运行 oracle 轮以保留诊断证据，但不会跳过原计划内的失败段继续，也不会将这两轮判为有效对照。所有失败尝试和恢复结果保留在各自输出目录。

主要输出：

- `REPORT.md`、`acceptance.json`：回放、提交与受控回收结果。
- `plan.json`、`results.jsonl`、`preparations.jsonl`：源行、操作和额外素材准备。
- `parp/oracle_updates.jsonl`、`switch-rankings.csv`、`switch-rankings.xlsx`：未来排序、预计 bin、cgroup 绑定和内核读回；段序号从 0 开始。
- `memory-timeline.jsonl`、`pressure-before/after.json`、`result.json`：逐应用内存、swap、refault、pgscan/pgsteal、major fault 和 PSI。
- `return-probes.jsonl`、`operation-probes.jsonl`：焦点到达与首次操作反馈耗时。
- `kernel-before/after/delta.json`、`pressure-kernel-before/after/delta.json`、`recovery-state.json`：全轮和压力时间窗内核 bin 证据及恢复审计。
- `performance-comparison.json`、`code-manifest.json`：同段响应记录对照及本次执行代码哈希。对照失效时不计算性能提升。

`COMPLETED` 只表示执行器没有报错；具体业务校验仍以每行 `detail.verification` 为准。继续采用已确认的本地网页、文档和媒体替代，不等同原网站内容或原始文件/内存负载。

```bash
python3 -m unittest discover -s test/test/tests -p 'test_oracle_next_use.py' -v
python3 -m unittest discover -s test/test/tests -p 'test_user_events*.py' -v
```
