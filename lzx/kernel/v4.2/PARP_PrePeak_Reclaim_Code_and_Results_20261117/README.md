# PARP 预测输出、提前回收与采集审阅包（2026-11-19）

**当前状态：待负责人审核，尚未开始补采或真实预测模型训练。** 本次在原GitHub文件夹内更新，保留2026-11-17源码、结果和历史说明。

负责人先看 [审阅要点与采集准入](04_负责人审阅与采集准入.md)，然后看 [最终预测接口](workspace/PARP_Prediction_Contract_Study_20261119/预测接口与代码改动.md)、[实验结果](workspace/PARP_Prediction_Contract_Study_20261119/实验结果.md)、[采集字段与步骤](workspace/PARP_Prediction_Contract_Study_20261119/交给采集人员的字段与步骤.md)。

## 最新代码与输出

最新代码：[`workspace/PARP_Prediction_Contract_Study_20261119/`](workspace/PARP_Prediction_Contract_Study_20261119/)。两类预测由脚本提供理想输出，完整调用应用间→应用内→提前回收；真实函数将沿用同一输出格式。

- 每0.5秒更新；默认从当前时刻起未来20秒，共40个0.5秒窗口。
- 每段保留 **WSS、实际占用增量、缺页次数**。缺页是本窗口计数，不是累计值。
- 同时给出当前安全冷量和有效期；实时空间、回收开始/截止和预算由控制器计算。
- 机器可读定义：[OUTPUT_CONTRACT.json](workspace/PARP_Prediction_Contract_Study_20261119/OUTPUT_CONTRACT.json)；实际原生输出示例：[OUTPUT_EXAMPLE.json](workspace/PARP_Prediction_Contract_Study_20261119/OUTPUT_EXAMPLE.json)。

## 已验证与边界

完成37项真实虚拟机检查：34轮完整运行（16轮对照、18轮理想预测）以及3次正确报告安全冷量不足。18轮理想预测的每次分配前缺口均为0。无热应用主动回收、无增长保护期主动回收、无OOM。峰值耗时存在波动，不宣称统一加速比例。

覆盖阶跃、缓慢增长、V形、倒V形、反复波动、工作集与占用解耦、同步释放、无缺口与重访保护等理想条件。12秒实际补足过空间但保守时间预算不合格；当前默认20秒，非普适最优。

验证的候选修正：下一峰值身份、按实际增长扣除已实现需求、按剩余安全冷量/预算判断可行性。原上传版controller的15个文件保持原哈希，修正通过外层适配及调度子类接入，并保留逐批已执行源码。

**本轮是整应用冷热、干净文件页的合成验证；固定high仍采用独立全序列参考包络。** 不代表真实预测准确率、最优历史长度、匿名压缩、页级定向回收或真实GUI卡顿已经验证。页访问采集器仍需按规格实现及预采检查；当前VM支持Page Idle，未启用DAMON。

## 审阅与复核入口

1. [代码与逻辑入口](01_代码逻辑说明.md)
2. [离线复核及运行](02_运行与验证.md)
3. [环境与尚缺能力](03_环境与依赖.md)
4. [负责人审阅与采集准入](04_负责人审阅与采集准入.md)
5. [采集人员规格](workspace/PARP_Prediction_Contract_Study_20261119/交给采集人员的字段与步骤.md)
6. [逐批结果与源码匹配](workspace/PARP_Prediction_Contract_Study_20261119/results/VERIFIED.json)、[执行源码快照](workspace/PARP_Prediction_Contract_Study_20261119/results/source_archives/)

旧版真实应用结果仍在 `workspace/parp-userspace-deficitfix-20261117/` 与 `summaries/`，原顶层说明归档在 [history/release-20261117/](history/release-20261117/)。旧版2/3补足、3.11MiB缺口等结论保持原样，与本轮不同场景分开。
