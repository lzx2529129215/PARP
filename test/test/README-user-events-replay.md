# user_events Excel 第一阶段回放

输入为 `/home/lzx/Desktop/user_events合并.xlsx`，包含 2615 条、12 组来源记录。应用 ID 沿用 `test/configs/source20_phase1` 的 30 应用空间，没有增加候选或开启预测。

本次完整回放与验收见 [执行报告](../../test_reports/user-events-phase1-replay-20260913.md)。最终 1316 条有后置校验、1008 条仅输入送达、291 条明确跳过；12 组完整遍历，无执行失败，离线审计通过。

## 映射与执行

在 PARP 根目录运行：

```bash
python3 test/test/user_events_plan.py \
  --xlsx /home/lzx/Desktop/user_events合并.xlsx \
  --output-dir test/configs/user_events_phase1

python3 test/test/user_events_replay.py \
  --xlsx /home/lzx/Desktop/user_events合并.xlsx \
  --output-dir test/outputs/user-events-new-run
```

回放输出目录必须不存在。`--groups 1 2` 可选来源组；`--rows 305 327 328 330` 仅用于调试，单独选行可能缺少前置状态。默认压缩等待；`--timing original` 按每组原时间差等待，仍不能声称应用和原始工作集等价。

中断后可对同一目录加 `--resume`，只复用来源哈希、映射和行序列一致的完整组。加 `--retry-failed` 会重跑含失败的组。部分执行或重试的旧目录保存在 `attempts/`，不会把旧结果覆盖成成功。

`test/configs/user_events_phase1/mapping.csv` 是可直接用表格软件查看的逐行映射，`plan.json` 保留来源文件、压缩包成员、源行号、原应用和时间。应用中心、文件管理器下误标的 WPS、CAD 等按操作文本及组内上下文处理；不会跨来源组继承上下文。桌面及跳过行仍保留，不能删除后拼接训练序列。

## 复现范围

- 浏览器→已有 FIREFOX 键（实际 Epiphany），海泰→Falkon。用本地长页、图片和视频代替原站点；加载、滚动有浏览器页面反馈。抖音视频流/评论页用同类本地浏览器页替换。
- WPS→WPS；腾讯文档→LibreOffice 本地文档。素材包含 40 页 PPT、Word、表格，不还原原 1 GB/百 MB 文件、上传和协作后台。
- 文件管理器→Nautilus；图库按动作使用 Nautilus 图片列表、EOG 图片查看和 VLC 视频播放；悟空图像→GIMP。原始 PSD 图层结构和图库裁剪范围未提供。
- 好压→File Roller，验证归档成员数和解压字节；备忘录→Mousepad，校验保存文本。
- 剪映→Shotcut，将视频追加四次并导出 60 FPS；校验工程四段、导出帧率、按时间线计算的时长并全帧解码。本地源视频为 640×360、30 FPS、约 5.7 秒，不还原 4K/120 FPS 工作集。
- QQ 音乐→Rhythmbox 本地播放；企业微信只打开/关闭 Kaidan 离线客户端。会议、飞书会议、小艺、虚拟机、CAD、原商店安装卸载、账号分享等逐行跳过。

部分菜单步骤用快捷键、应用原生命令或 MPRIS 替换，日志注明 `gui=false`。启动步骤允许复用已有进程，不宣称每条“冷启动”都恢复原始冷启动条件。WPS“下载”保存位置改为独立素材目录中的新文件名。

EOG 的文件选择器在本机不能稳定切换图片，因此通过原生命令重新打开指定图片，并用标题栏 OCR 校验文件名；这会重启图片查看器。浏览器标签按内容身份和可见性反馈选取，不依赖标签编号快捷键或假设新标签总是在末尾。

WPS 的两种文件打开面板行为不一致，文档打开步骤通过 WPS 原生命令重启本次专用实例并加载指定文件，校验文档标题和编辑窗口。每次重开使用新配置，旧配置保存在 `previous-session-*`；不会保留此前文档标签，后续编辑仍在 WPS 窗口内执行。WPS 表格使用项目已有的 `spreadsheet_0060.xlsx`，LibreOffice 使用独立文档副本及扩展表格，避免文件锁冲突。

## 证据和结果解释

根目录 `report.md`、`execution.csv`、`summary.json` 汇总实际执行；计划覆盖数和执行成功数分开。

| 状态 | 含义 |
|---|---|
| VERIFIED | 页面反馈、输出文件或媒体状态通过后置校验 |
| INPUT_SENT | 输入已送达所属窗口，不代表每个业务状态都验证成功 |
| SKIPPED | 明确排除或不能等价替换，原行仍保留 |
| FAILED | 实际尝试失败，保留错误和截图 |
| SKIPPED_PREREQUISITE | 前置文档未加载，避免把后续动作发给旧文档 |

每组 `results.jsonl`、`inputs.jsonl` 保留逐行动作；`screenshots/` 保留界面及窗口/进程归属；`web-telemetry.jsonl` 记录本地网页加载、位置和可见性；`fixtures/` 保留原素材副本及输出文件。每次运行重新建立独立 Xvfb/Openbox 桌面，所有输入只发到本次所属窗口。

应用在所属 `parp-<app>.slice` 下创建本次专用 service，兼容常驻 PARP 进程路由；每组 `runtime_app_scope.json` 记录实际绑定。`process-audit.json`、`cleanup.json` 核验进程归属和清理。常驻 PARP 服务可能观察进程；本执行器不写入模型或内核回收控制。

本机依赖已在第一阶段及现有 30 应用环境安装：Python 3、Pillow、Xlib、Xvfb、Openbox、systemd 用户服务、xdotool、wmctrl、xclip、ffmpeg、gdbus、Tesseract 及映射应用。XLSX 读取使用标准库，不依赖 Excel、pandas 或 openpyxl。

## 检查

```bash
python3 -m unittest discover -s test/test/tests -p 'test_user_events*.py' -v
python3 test/test/audit_user_events_replay.py test/outputs/user-events-new-run
```

测试覆盖完整导入、来源边界、误标应用、CAD 上下文、会议和上传/安装的跳过规则，以及错误窗口、陈旧页面反馈和标签顺序等实际发现的问题。最后的只读审计检查全量行数、输出文件和清理；GUI 与输出文件的验收以实际回放证据为准。
