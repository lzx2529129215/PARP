# user_events 第一阶段映射与实际回放

已按现有 30 应用空间完成 Excel 的逐行映射，并在独立 Xvfb/Openbox 桌面实际遍历全部 **12 组、2615 条记录**。源文件未修改，组内顺序、原始行号、时间差及来源信息均保留。

| 最终状态 | 条数 | 解释 |
|---|---:|---|
| VERIFIED | 1316 | 页面反馈、保存文件或媒体状态等后置校验通过 |
| INPUT_SENT | 1008 | 输入已送到所属窗口，未逐项验证业务结果 |
| SKIPPED | 291 | 不接入第二阶段，或缺少原始账号、素材、平台能力等 |
| FAILED / SKIPPED_PREREQUISITE | 0 | 最终保留的完整组无执行失败或前置失败 |

2324 条进入替代执行，占源记录的 88.9%；这不是全部业务结果已验证的比例。此前调试和重试失败的证据保存在输出目录 `attempts/`，没有覆盖成成功。

- [20 个来源应用覆盖表](../test/configs/user_events_phase1/coverage.md)
- [2615 条逐行映射 CSV](../test/configs/user_events_phase1/mapping.csv)
- [分应用执行结果](../test/outputs/user-events-replay-20260913-v2/report.md)
- [逐行执行状态 CSV](../test/outputs/user-events-replay-20260913-v2/execution.csv)
- [全量审计及产物清单](../test/outputs/user-events-replay-20260913-v2/audit.json)
- [执行器和运行说明](../test/test/README-user-events-replay.md)

主要替代关系：浏览器使用已有 FIREFOX 键对应的 Epiphany，海泰使用 Falkon；腾讯文档使用 LibreOffice；文件管理使用 Nautilus；图库按动作使用 Nautilus、EOG 和 VLC；悟空图像使用 GIMP；好压使用 File Roller；备忘录使用 Mousepad；剪映使用 Shotcut；QQ 音乐使用 Rhythmbox。WPS 仍由 WPS 执行。

网页、文档、图片及媒体使用有限本地素材，默认压缩等待。WPS 文档打开可重启专用实例并使用新配置，不保留此前文档标签；EOG 图片切换也采用原生命令重开。部分压缩、播放等动作通过应用命令或 MPRIS 执行，日志注明 `gui=false`。因此不声称完全等同原 GUI 操作路径、在线服务、冷启动条件、原大文件大小或内存负载。

会议、虚拟机、AI 助手等第二阶段内容未接入；具体跳过原因均在逐行映射中。此次未执行模型训练、预测验证或内核回收实验。

验收：9 项导入、映射和窗口保护测试通过；全量审计核对 2615 行及 12 组顺序、源文件哈希、23 项输出/筛选检查和进程清理，错误数为 0。56 个应用运行单元均已停止。视频工程含四段素材，导出 60 FPS，时长与 22.8 秒时间线相符，整段解码通过；Word 内容、GIMP XCF、笔记、复制及归档产物均有相应证据。审计通过不会把 INPUT_SENT 升级为业务结果已验证。

运行新一轮：

```bash
cd /home/lzx/Desktop/PARP
python3 test/test/user_events_replay.py \
  --xlsx /home/lzx/Desktop/user_events合并.xlsx \
  --output-dir test/outputs/user-events-new-run
python3 test/test/audit_user_events_replay.py test/outputs/user-events-new-run
```

新输出目录须不存在；中断续跑使用 `--resume`，重试失败组再加 `--retry-failed`。`--timing original` 可按组内原时间差等待；默认压缩模式只验证可执行的操作流程。

源文件 SHA-256：`f6f26ee836d98ca0b84f494418daf0d0bac27c182b82b0138709a060362b47ca`。
