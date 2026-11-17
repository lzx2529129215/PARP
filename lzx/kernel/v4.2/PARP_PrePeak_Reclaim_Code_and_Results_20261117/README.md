# PARP 最新代码、逻辑说明与结果（2026-11-17）

本包对应最后完成的 **3854 MiB、真实缺口、revisit 场景**批次。最新主代码只有一份：

[`workspace/parp-userspace-deficitfix-20261117/`](workspace/parp-userspace-deficitfix-20261117/)

代码与原始结果原样复制，未修改本批已经冻结的运行代码，也未重新启动虚拟机实验。其余旧日期目录是必要依赖、历史校准证据或中止记录。

## 阅读顺序

1. [代码逻辑说明](01_代码逻辑说明.md)：输入、WSS 固定 high、细粒度 reclaim、截止时间、0.5 秒更新和保护机制。
2. [实验结果](summaries/实验结果.md)：设置、9 轮明细、正式对照、缺页、未达到的部分。
3. [运行与验证](02_运行与验证.md)：离线检查、结果再生成、另建目录后运行新实验。
4. [环境与依赖](03_环境与依赖.md)：包内依赖和仍需现有 Ubuntu 虚拟机提供的程序、资产。

## 当前结论

- 正式开启组 2/3 轮在第 8 秒主峰开始前补足空间，另一轮仍缺 **3.11 MiB**，因此目标没有全部达成。
- 正式组峰值响应 P95 均值 **230.03 ms**，关闭组 **225.50 ms**，观察到约 **+2.01%**；每组仅 3 轮。
- 各应用完整任务总 pgfault 增幅均低于 10%。这项通过不代替空间、响应或大缺页诊断。
- 全部开启轮在主峰统计区间没有发起 memory.reclaim，也没有调用越过截止时间；Firefox 仍有 high 压力事件。

## 目录

```text
workspace/
  parp-userspace-deficitfix-20261117/  最新主代码、测试、原始 results
  parp-measured-validation-20261113/ 历史源码和候选额度证据
  oracle-realapps-validation-20261110/ 原生 GIMP 和输入探针辅助代码
  foreground_krita_gui/             Krita GUI 辅助包、配置与依赖清单
  krita-vm-acceptance/              cgroup 启动、临时任务通道等必要辅助代码
  parp-userspace-four-ops/           核验用原始操作控制器
  parp-wss-v5/                      任务通道核验所需的 10 个历史源码文件
summaries/                         可读结果及机器可读汇总
evidence/                          本批实时日志、宿主机采样
history/                           3918 MiB 中止批次，与正式汇总分开
tools/                             汇总、完整性校验、新实验目录准备工具
manifest.json                      包内文件 SHA-256 清单
validation.json                    整理后的本地验证记录
```

原始结果中的 `/home/qi/...` 等绝对路径是采集时的记录，保留原样；它们不表示本包在当前机器上的位置。复核结果直接使用包内相对路径即可。

`TOTAL_QUALIFICATION-revisit.json` 是总缺页校准资格记录，里面 `latency_qualified` 和 `space_target_qualified` 均为 false；不能把文件存在或 eligible=true 当作整个研究目标已经完成。

## 快速复核

在本文件所在目录执行：

```text
python -X utf8 -B tools/verify_bundle.py
python -X utf8 -B tools/summarize_results.py
```

两个命令均不启动原生应用、不操作 cgroup、不需要 sudo。第二个命令只重建 `summaries/` 的派生结果，不改原始记录。
