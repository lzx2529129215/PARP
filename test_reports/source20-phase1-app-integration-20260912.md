# 第一阶段应用接入验收（2026-09-12）

WPS、File Roller、Mousepad、Shotcut 四项完整 GUI 验收通过。

| 应用 | 内容验证 | 切回 / 归属 / 清理 |
|---|---|---|
| WPS | DOCX 中成功追加并保存指定文字 | 全部通过 |
| File Roller | GUI 解压结果内容与 SHA-256 一致 | 全部通过 |
| Mousepad | 文本追加保存成功 | 全部通过 |
| Shotcut | 173 帧在第 60 帧分为两段，范围连续；1080p 导出时长 5.782 秒，完整解码通过 | 全部通过，含 melt-7 导出进程 |

新增安装 Mousepad、Shotcut、FFmpeg（及软件源依赖），复用本机 WPS 和 File Roller。
独立版本维持 30 个真实应用名额，用四项替换纸牌、Claws Mail、地图、扫雷。
旧 LSApp 词表、checkpoint、常驻服务未切换。新版本尚无匹配 checkpoint，预测禁用。
会议、虚拟机、助手不在此次范围，其余 26 应用没有重测。

运行说明：[README](../test/test/README-source20-phase1.md)。
机器可读摘要：[acceptance_summary.json](../test/configs/source20_phase1/acceptance_summary.json)。
原始验收：[acceptance.json](../test/outputs/source20-phase1-20260912-final/acceptance.json)。

4 个配置/内容校验单元测试通过；所有本轮应用与桌面服务退出，无残留。
修复了 WPS 快速模拟按键重复、延迟字体弹窗、Shotcut 软件绘图后端崩溃、
原生文件对话框和时间线追加期间保存重入问题。另卸载一处已失联的旧实验 GVfs 挂载，
操作记录保存在配置目录 environment_repair.json。

本报告仅证明当前软件版本与固定桌面尺寸下的基本可重复操作，
不代表这 20 个来源应用已完整覆盖，也不代表预测或内存回收性能得到验证。
