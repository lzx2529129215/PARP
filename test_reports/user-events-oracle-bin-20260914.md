# 已知未来序列驱动 PARP bin：实现与验收

后续更新：已接入[WPS回放崩溃修复](wps-replay-crash-fix-20260914.md)。以下保留原压力尝试的失败结果，不将后续专项验证追认为原对照通过。

已实现持续回放、未来排序适配器、原子提交/读回、受控双轮压力实验和独立恢复。**完整 85 段压力对照尚未通过，不能声称性能改善。**

## 交付入口

- [持续回放与受控实验](../test/test/user_events_oracle.py)，由 `user_events_replay.py --continuous` 调用。
- [序列编译与内核排序适配器](../test/test/oracle_next_use.py)。
- [运行说明](../test/test/README-user-events-oracle.md)。
- [测试](../test/test/tests/test_oracle_next_use.py)：排序、段游标、前台确认、绑定歧义、读回/续租冲突、压力门槛与恢复。

```bash
cd /home/lzx/Desktop/PARP
python3 test/test/user_events_replay.py --continuous run \
  --output-dir test/outputs/oracle-new-pair
```

输出目录必须不存在。默认运行完整 native/oracle 两轮，不分来源组。原来的分组回放入口保留。输入来自对比 Excel 逐行记录，并用原始工作簿重新生成/校验操作参数；并非读取只剩应用名的 TXT。

当前执行计划为 85 段、2230 条应用内操作。计划 SHA-256 为 `b5151c91added100fe04e17278d9d8b812fd2f1ab421cbd501f186faab5359f2`。继续使用已有本地替代素材。应用市场的通用启动改用本地已安装应用页；没有还原真实新浪微博等网站。

## 三层验收

| 层次 | 结论 | 证据范围 |
|---|---|---|
| 排序正确 | 已通过自动化测试和已发布批次离线复核 | 下一次未来出现、无限远、前台排除、存活过滤、重复应用、跨组连续合并、同段重开等；不把尚未执行的段称为已回放 |
| 提交生效 | 已验证真实 ioctl/GET_STATE | 早期两段冒烟包含 88 批真实读回；压力尝试 v4 的 oracle 包含 292 批，逐批核对 rank、绑定、TTL 和 generation |
| 回收闭环 | 有实际回收和 bin 使用证据；完整对照未通过 | v4 oracle 完成 19 段、559 条操作后 WPS 崩溃；基线也已失效，因此不形成有效完整对照 |

后台展示顺序是“不再出现 → 较晚 → 较早”；内核 rank 方向相反，前台 rank=1/32767，后台 rank=2…N/基础分0。顺序分数不是概率。使用现有 ABI v3 和 8-bin 算法，禁用本轮概率捷径及 WSS 附加预测；不调用 LSTM。

运行时只接受属于本次实验 cgroup 的前台窗口。失败后停止该轮和续租，不跳过失败段。1 秒续租、5 秒 TTL；重开/辅助进程绑定变化只补发当前游标。控制器及 guardian 位于受压子树之外。

## 压力尝试 v4 的实际证据

[完整尝试报告](../test/outputs/oracle-pair-20260914-v4/REPORT.md) · [逐次排序 Excel](../test/outputs/oracle-pair-20260914-v4/oracle/switch-rankings.xlsx) · [内核提交日志](../test/outputs/oracle-pair-20260914-v4/oracle/parp/oracle_updates.jsonl) · [逐应用压力数据](../test/outputs/oracle-pair-20260914-v4/oracle/result.json)

- 压力边界：第 4 段（内部游标3）；基线用量 2,100,396,032 字节，oracle 用量 2,010,083,328 字节，相差约4.3%。
- 两轮使用同一 `memory.max=1,899,069,440` 字节；oracle 保持60秒后解除。基线因采集竞态提前结束，不能作为有效对照。
- oracle 压力窗口：pgscan +641,809，pgsteal +397,457，major fault +5,634，swap +862,728,192 字节；更多逐应用/refault/PSI 数据见 result.json。
- oracle 整轮内核计数：context_hits/rank_scores 各 +65,951，rebin_moves +12，subtree_selected +26,298。**这些历史计数覆盖整轮，是全局计数；不能据此推断严格的逐应用回收次序。** 最新执行器另采集压力时间窗的 kernel before/after/delta，避免将施压前的上下文读取混入该窗口验收。
- oracle 在原 Excel 第694行打开替代表格时失败。WPS 服务记录退出码139：[故障日志](../test/outputs/oracle-pair-20260914-v4/oracle/wps-failure-journal.txt)。该行停止，没有跳过或伪称完成。
- 基线的短命辅助进程/cgroup 采集竞态已补充重新采集处理；重试仅针对路径消失，不重试绑定歧义、提交失败或应用崩溃。应用市场本地启动已在该 oracle 轮验证通过。

此前 v1/v2 的 WPS 故障、v3 的应用市场超时和超过15%内存偏差均保留在相应输出目录，未用后续尝试覆盖失败证据。失败基线后的 oracle 轮仅用于独立诊断，不会使失效对照变为通过。

## 恢复与限制

实验保存并恢复原内核控制值和常驻 `parp-runtime-monitor.service` 的活动状态，退出时清理自己的应用、桌面和 cgroup。guardian 使用 pidfd 处理主执行器异常退出；恢复记录写入 recovery-state.json。已有 SIGKILL 故障注入验证：[恢复记录](../test/outputs/oracle-recovery-20260914-v1/acceptance.json)。

`COMPLETED` 表示该操作的执行器调用没有报错；业务结果验证依据每行 detail.verification。响应记录是焦点到达和首次操作耗时，不能代替业务成功率。性能比较仅在完整有效双轮对照通过后汇总，目前不声称 oracle 优于 native。

当前阻碍完整验收的是 WPS 在持续会话的表格重开过程中真实崩溃。需要先稳定这一应用操作，再取得完整85段的有效双轮证据；不能用已验证的 ioctl 或局部回收替代这一验收。

## 最后检查

- 自动化测试：19 项排序/控制/恢复相关测试和 9 项原回放测试，共28项通过。
- [7段双轮冒烟](../test/outputs/oracle-smoke-20260914-v3/REPORT.md)：两轮各214条操作通过；oracle 共127批真实提交及读回通过。该无压力检查不计入完整回收验收。
- [最新排序 Excel](../test/outputs/oracle-smoke-20260914-v3/oracle/switch-rankings.xlsx) · [含实际读回字段的内核日志](../test/outputs/oracle-smoke-20260914-v3/oracle/parp/oracle_updates.jsonl)。
- [带私有桌面和实验 cgroup 的 SIGKILL 恢复测试](../test/outputs/oracle-recovery-20260914-v2/acceptance.json)通过：桌面无存活进程、测试单元 inactive、服务恢复 active，无恢复错误。该测试只启动空闲进程验证资源恢复，不是内存压力性能实验。
- [最终环境检查](user-events-oracle-final-check.json)：原控制值匹配、常驻服务 active、本次冒烟单元均已停止。
- 每轮 `code-manifest.json` 记录该轮实际执行版本；后续增加的异常窗口重启防护、空父 cgroup 完整绑定由回归测试覆盖，未宣称全85段 GUI 已通过。

## 补充：WPS 崩溃定位

内核日志进一步确认：11:55:19，wpscloudsvr 在 libqingbangong.so 中发生段错误；11:55:20，wpsoffice 在 libQt5CoreKso.so.5.12.10 中发生段错误。两者先后发生，尚不能仅凭时间顺序断定前者导致后者。直接证据见 [内核崩溃日志](../test/outputs/oracle-pair-20260914-v4/oracle/wps-kernel-segfault.txt)。

实验 cgroup 的 oom/oom_kill/oom_group_kill 均为0，压力已在本次主进程崩溃约5分钟前解除；实际替代表格只有10,487字节。因此没有证据把这次失败归为 OOM 杀进程或直接打开原始100MB文件造成的容量不足。更深的触发原因（WPS自身缺陷、隔离会话/重开兼容性、或其他因素）仍需崩溃堆栈及隔离复现来区分；现有证据不能证明由 bin 排序导致，也不能完全排除内存压力历史的影响。
