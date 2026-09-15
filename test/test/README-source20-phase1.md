# 20 来源应用：第一阶段接入

状态：2026-09-12 四应用完整 GUI 验收通过，内容、切回、归属、清理均通过。
验收目录为 `test/outputs/source20-phase1-20260912-final`，
摘要为 `test/configs/source20_phase1/acceptance_summary.json`。
其余 26 个应用本轮未重测。

本版本为图中 20 个来源应用提供独立的 30 应用实验空间，并接入 WPS、
File Roller、Mousepad、Shotcut 的隔离 GUI 回放。第二阶段的虚拟机、
会议、助手不在本次范围内。

## 应用空间

配置目录：`test/configs/source20_phase1/`。

| runtime ID | 新应用 | 对应来源 | 本版本替换的旧项 |
|---|---|---|---|
| 15 | WPS | WPS | Solitaire |
| 25 | FileRoller | 好压 | ClawsMail |
| 28 | Mousepad | 备忘录 | Marble |
| 29 | Shotcut | 剪映 | GNOMEMines |

其余 26 个应用的 ID 保留，真实应用总数为 30，词表索引为 runtime ID − 1，
PAD=30、UNKNOWN=31。旧 `lsapp_30` 配置、映射、checkpoint 不修改。
这些 ID 在新版本中有不同语义，不能跨版本混用。

- `runtime_app_scope.json`：可由现有 RuntimeAppScope 解析器读取。
- `app_vocab_duration.json`：新空间的独立词表。
- `automation_manifest.json`：接入范围和验收索引。
- `source_app_mapping.json`：20 来源应用的映射建议及部分覆盖边界，
  不是已完成数据导入的声明。

本轮是自动化接入，未获得这 20 应用的原始事件数据，未重训预测器、
切换常驻服务或进行内核回收性能实验。新配置全部 `prediction_enabled=false`；
接入匹配词表的新 checkpoint 后才能另行启用预测。

## 安装依赖

本机已有 WPS 和 File Roller。本次安装 Mousepad、Shotcut，以及用于导出结果
验证的 FFmpeg。Ubuntu 软件源安装方式：

```bash
sudo apt-get install --no-install-recommends mousepad shotcut ffmpeg
```

沿用框架已有的 Python Pillow、Xvfb、Openbox、xdotool、wmctrl、
xclip、dbus-run-session 和用户 systemd。

## 运行

在 PARP 根目录执行；输出目录必须全新。

```bash
python3 test/test/source20_phase1_gui.py --check-config
python3 test/test/source20_phase1_gui.py \
  --output-dir test/outputs/source20-phase1-NEW
```

可用 `--apps WPS FILE_ROLLER MOUSEPAD SHOTCUT` 选择子集。单应用运行不构成
跨应用切回验收；完整四应用运行会依次切走、切回并校验窗口 PID 的 cgroup。
自动化结束后停止本轮自有应用服务与隔离桌面，清理短路径链接。

## 操作和验收

| 应用 | 本轮回放 | 内容断言 |
|---|---|---|
| WPS | 打开 DOCX 副本、定位末尾、追加文字、保存 | 读取 DOCX 的 `word/document.xml`，校验新增文字 |
| File Roller | 打开 ZIP、点击解压、选择私有目标目录 | 解压文件内容和 SHA-256 一致 |
| Mousepad | 打开文本副本、追加文字、保存 | 保存后的文本含新增文字 |
| Shotcut | 视频加入时间线、保存、在第 60 帧分割、预览、导出 | MLT 两段连续无丢帧，切点为 2 秒；导出视频时长正确且全部帧可解码 |

每项还检查应用内容窗口、进程归属、退出后的残留进程。
Shotcut 导出时采样 `melt`/`melt-7`/`qmelt`/`ffmpeg` 子进程，验证归入 Shotcut 服务。
所有按键和鼠标事件发出前检查当前活动窗口归属。

WPS 使用已有运行方式 `wpsoffice /prometheus`，采用私有配置中的融合模式和
首次许可设置，不复制用户账户或恢复会话。等待嵌入式编辑窗口后再操作，文字通过隔离 X 桌面的剪贴板粘贴，
避免 WPS 重复接收快速模拟键盘字符。
处理字体检查和默认程序提示时不修改系统默认程序。
它仍可能提示缺少某些公式字体；本轮校验普通文本，不声称验证公式排版。

Shotcut 使用软件 OpenGL、Qt 文件对话框和 Fusion 样式，固定 1280×900
隔离桌面。File Roller 的解压按钮、Shotcut 的导出按钮使用已验收的固定位置；
更换版本、样式或桌面尺寸必须重新验收。时间线追加后等待缩略图和波形任务完成，
避免保存操作重入追加任务。

Shotcut 不使用会导致当前安装版本崩溃的
`QT_QUICK_BACKEND=software`。短素材导出前检查至少 1 GiB 空闲空间，
私有 Shotcut 配置关闭该版本默认的磁盘空间询问，避免固定短任务被询问框阻塞。

WPS、Shotcut 的界面就绪必须区别于窗口标题出现。GUI 验收不等于
WPS 全组件、剪映全部功能或来源应用内存工作集等价。
已有更丰富的 WPS 场景继续见 `test/automation/README_WPS.md`。

## 产物

- `acceptance.json`：每应用状态、内容断言、焦点切换、清理结果；
  `full_phase1=true` 才代表本轮四项均完成。
- `APP/actions.jsonl`、编号 PNG/JSON：带时间戳的操作、窗口和截图证据。
- `APP/process-audit.json`、`APP/cleanup.json`：归属和清理证据。
- `SHOTCUT/export-audit.json`：导出视频元信息及导出子进程。
- `fixtures/`：仅本轮使用的文档、压缩包、文本、视频、工程和导出文件。
- `runtime_app_scope.json`：本轮实际 transient service 绑定，可用于对接观测；
  进程已经清理后，这些 service 名称只是本轮证据，不是常驻服务。

脚本复用 `lsapp_30_gui.py` 与 `visit_window_scenarios.py`。短 XDG 链接指向
本轮证据目录，归属审计同时识别短路径环境，避免遗漏迁移到服务之外的辅助进程。

## 检查

```bash
python3 -m unittest discover -s test/test/tests -p test_source20_phase1.py -v
```

这些检查会拒绝未编辑的时间线、分割丢帧/重叠、错误切点，以及只在 DOCX
元数据中出现验证文字的假阳性。GUI 回放仍需单独执行。

本次另清理一处旧实验遗留且已返回 ENOTCONN 的失联 GVfs 挂载，
未删除该目录下的普通文件。处理记录在
`test/configs/source20_phase1/environment_repair.json`。软件版本记录在同目录
`package_versions.json`。
