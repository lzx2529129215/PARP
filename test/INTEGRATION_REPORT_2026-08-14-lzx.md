# PARP 目录迁移与联调报告

> 采集时间：2026-08-14；内核：`6.17.13-parp-lzx-v4.2+`。`lzx-note`

## 当前责任边界

- `test/automation`：只负责可重放的 GUI/应用操作和 action trace。
- `test/test`：独立管理 cgroup、tracepoint、内存 sidecar、安全中止和指标汇总。
- `lzx/service/runtime_monitor`：只观察常驻服务，跨实验 slice 发现 15 个 LSAPP 对齐应用，不写 debugfs、不 reclaim、不修改内核开关。
- `lzx/tool/operation_predictor`：保留模型、词表和训练流水线。

## 常驻服务

- unit：`parp-runtime-monitor.service`
- 状态：`enabled` / `active (running)`
- user linger：`yes`
- 启动后重启次数：`0`
- 默认使用 `runtime_app_scope.service.json`，不绑定单一 test slice。
- Evince 跨 cgroup 在线复核：`evince|evinced` 两进程合计 `44,957,696` bytes，不再误读整个父 slice。

## 自动化结果

六应用隔离 Xvfb 场景：`PASS`。Calendar、Rhythmbox、Image Viewer、Shotwell、System Monitor、Solitaire 全部启动成功，每个应用有 4 次焦点记录，失败动作为 0。

- 验证结果：`test/outputs/automation_login_free/integration_cross_slice_20260814/verification.json`
- action trace：`test/outputs/automation_login_free/integration_cross_slice_20260814/automation_trace.csv`

## 独立 test 诊断轮

配置：LSAPP-expanded 15 应用、hot/cold smoke、seed `20260814`、observe 模式、18 个切换步骤。结果为 `VALID_DIAGNOSTIC`，1/1 轮有效：

- 应用启动：15/15；计分步骤：18/18；失败动作：0。
- trace `page_fault_user`：10,241；trace 读取事件：10,257；丢失：0。
- test cgroup `pgfault/pgmajfault`：779,309 / 7,397。
- file/anon refault：34,673 / 6,275。
- `pgscan/pgsteal`：325,197 / 252,968；扫描效率：77.789%。
- cgroup OOM / OOM kill：0 / 0；宿主 OOM kill 增量：0。
- 启动就绪延迟均值/P95：1,427.50 / 3,148.93 ms。
- 最低 `MemAvailable`：1,416,007,680 bytes；监控样本：69。

原始结果：`test/outputs/parp_acceptance_lsapp_aligned/hotcold-smoke-observe-20260814_192137-6.17.13-parp-lzx-v4.2+/summary.json`。

## 结论边界

这是目录、应用、tracepoint、cgroup 和服务数据链的有效 smoke，不是优化改善率。本轮逻辑负载仅为物理内存的 5%，不满足 full 的 150%–200% 压力契约；当前 effective-tier 模型来源仍为 `ENGINEERING_FIXTURE_UNTRAINED`，也没有同随机序列 Native/OFF 与 Apply 配对，因此不能由这轮声称 PageFault 已下降。OOM 为 0 同样只能说明 smoke 安全完成，不能评价 OOM 降低比例。
