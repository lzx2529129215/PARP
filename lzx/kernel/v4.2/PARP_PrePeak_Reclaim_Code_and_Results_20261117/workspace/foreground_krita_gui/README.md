# Krita 5.0.2 / 5.2.2 外部真实桌面自动化

本模块操作 Ubuntu X11 桌面：鼠标、键盘、滚轮、OCR、窗口属性。没有 Krita Python 插件/API，没有图像处理替代负载，没有 cgroup、内存采样或新内存限制。不导入或修改旧 foreground_krita、后台 images、sort、xz。内存实验由相邻 krita-vm-acceptance 外部工具负责。

**2026-11-01 已在本地 Ubuntu 22.04.4 / Linux 6.17.13 / 原生 Krita 5.0.2 / 固定 Xephyr X11 桌面完成真实 16 阶段功能轮，退出码 0，素材摘要不变。** 使用独立生成的 4096²×4 层功能 ORA；48 层压缩实验结果及稳定性以相邻 krita-vm-acceptance/ACCEPTANCE.md 为准。历史 Ubuntu 24.04/XFCE/224 层目标不据此标记通过。默认素材路径和 SHA-256 保持 null，须锁定本轮素材后运行；文件大小、raw RGBA 和真实 RSS 分别记录。

## Qt 环境冲突修复版本

用户提供的失败轮次 e91772c98c1d4616879a1f0dc5b4666e 日志显示，Krita 尝试加载虚拟环境 cv2/qt/plugins 中的 xcb 插件。本环境未访问或修改该轮次。

process_manager.launch 在每次 Popen 边界重新复制父环境，移除 QT_QPA_PLATFORM_PLUGIN_PATH、QT_PLUGIN_PATH、QT_QPA_FONTDIR，让原生 Krita 使用自身 Qt 插件搜索路径，不硬编码替代 Qt 目录。版本预检也使用同样的清理副本。自动化父进程仍保留其 OpenCV 环境；不修改系统或桌面会话环境。

QT_QPA_PLATFORM 只允许未设置、空值或 xcb；offscreen、minimal、Wayland、回退列表及未知后端均安全失败。DISPLAY、XAUTHORITY、XDG_RUNTIME_DIR、DBUS_SESSION_BUS_ADDRESS 保持继承；原来未设 XAUTHORITY 时仍保留已有 .Xauthority 回退逻辑。系统资源路径与私有 HOME/XDG 隔离不变。

每轮 qt-environment.json 和 automation.log 记录清理前后的变量是否存在、值的 SHA-256、移除键名、平台判定和会话变量是否保持。不记录路径原值、认证内容、会话总线地址或全量环境。被拒绝的平台先写审计再停止。环境审计不能替代真实 PID/窗口验证。

首次部署先执行 `bash scripts/run_gui.sh --config configs/gui.vm.json --startup-only`。此入口仍校验锁定 ORA 的只读元数据与摘要，但只运行 LAUNCH_KRITA、VERIFY_KRITA 两个原有状态，不打开素材。成功后 status=STARTUP_VERIFIED、startup_test=PASS，完整 GUI 的 valid=false、gui_test=NOT_RUN；进程正常退出仍未验证。窗口有意保留，按 process.json 核对后手动关闭本轮空白窗口，再执行完整流程。启动失败也保留现场，不自动杀进程。未使用该选项时仍执行原有全部 16 个状态。

本次独立包为 foreground_krita_gui_qt_env_fix_bundle.zip。旧包和旧失败记录保持原样；不要将旧包再次解压到新版目录。指定素材锁定、部署及两阶段验证命令见 DEPLOY.md。

## 素材：仅查验已有文件

默认只搜索 `~/oom-stage-a/krita_gimp_lab/assets`，读取现有 ORA 的 mimetype、stack.xml、引用清单并流式计算 SHA-256。绝不生成、提取、重压缩或改写素材。候选门槛默认 4096² 像素，并排除文件名含 smoke 的文件；门槛可配置，只是候选筛选，不是内存结论。

```bash
python3 -m oomlab_krita_gui.cli inspect
python3 -m oomlab_krita_gui.cli inspect --select-config configs/gui.selected.json
```

必须恰好一个合格候选，否则返回 2、列出清单且不写配置。锁定配置也拒绝覆盖。用户核对它确实是之前指定的大素材后，在全新部署中替换默认未配置模板：

```bash
cp configs/gui.selected.json configs/gui.json
```

最终 configs/gui.json 保存实际绝对路径和摘要。每轮重新校验；没有锁定素材、摘要变化或小素材会在启动 GUI 前停止。未知路径不会自动生成，不能通过降低门槛将小素材冒充目标。

## 运行顺序与独立验证

状态机固定 16 个动作/验证状态，不按尺寸跳步骤：

1. 启动 Krita：无文件参数，私有 HOME 和 XDG 用户目录；取得本轮子进程 PID/starttime，等待其独立可见窗口。
2. 验证实例：wmctrl、xprop、xwininfo 与真实 /proc PID/父进程/starttime、原生可执行文件匹配；不能选择已有窗口。
3. Ctrl+O，视觉确认文件对话框，通过键盘输入锁定路径并回车。
4. 等待文档标题、状态栏尺寸、Layers 面板和非空画布图像稳定出现。
5. 鼠标双击标题栏最大化；若原已最大化，先双击恢复再双击最大化。通过窗口状态和桌面工作区比例验证。
6. 画布中心真实鼠标滚轮放大，再以 OCR 百分比增加及画布截图变化验证。
7. 同样滚轮缩小，验证百分比降低及画布变化。
8. 鼠标 Layer → New → Paint Layer，新建后验证新增行名称、蓝色选择高亮、面板变化及文档修改标记。
9. Ctrl+W，识别同 PID 的保存提示和唯一 Discard/Don't Save 文字位置，用鼠标点击；确认返回空白/欢迎工作区，原 ORA 摘要不变，运行目录和素材目录没有新建/改写 KRA。
10. 确认文档已关闭后 Ctrl+Q，等待真实退出，要求退出码 0、无本轮窗口、无已观察到的本轮后代残留，再复核原素材摘要。

新增图层使用菜单这一允许的真实鼠标方案，不调用内部 API。若面板无法显示总层数，记录“视觉新增行”，不把原层数+1 的理论值冒充实测总层数。KRA 检查覆盖本轮运行目录及素材目录，不能声称审计整个文件系统。进程后代按阶段采样，可能错过在两个观察点之间出生又退出的瞬时子进程；不声称完整内核生命周期追踪。

动作与其下一验证状态构成一个完整操作。输入已发送的状态会标记 verification_pending，不意味着操作已成功；最终 COMPLETE 要求所有验证状态完成。每阶段记录 wall/monotonic 时间、success、verification、error 和前后截图。任何异常都会停止后续输入，保存错误截图与窗口状态，不强制关闭或杀死 Krita，不修改旧结果。用户需检查保留现场后手动处理本轮实例。

## 视觉校准与安全失败

默认英语标签用于常见 Krita 布局；当前 5.0.2 运行配置由相邻 run_gui_job.py 生成，保存问题是完整的 Yes/No/Cancel 形式，验证问题后识别并点击 No。OCR 找不到、置信度低、有多个匹配、窗口移出可截图桌面、主题不支持蓝色选择判据都会失败，不会盲点坐标或跳过验证。截图通过 Pillow X11 抓取绑定输入 DISPLAY；菜单仅从同 PID 的实际可见 popup 读取，主窗口和对话框均使用 xwininfo 客户区坐标。

在 configs/gui.json 中调整 regions（归一化窗口区域）、labels、ocr_language、ocr_confidence、超时和滚轮刻度。menu 覆盖主菜单栏，popup 配置保留兼容但菜单定位以原生 popup 范围为准。canvas 应避开工具栏/面板；layers 应覆盖图层行和标题；status 应覆盖缩放比例及尺寸。OCR 语言须安装对应 Tesseract 包。当前选择高亮判据只支持蓝色选中行，灰色/其他主题须先扩展并测试视觉验证器，不能直接关闭判据。高内存黄色状态栏的尺寸 OCR 另取截图蓝通道增强文字对比，仍要求置信度 55 和精确尺寸匹配；原始截图不改写。前后画布差异阈值由 change_threshold 控制。

所有点击点来自当前窗口几何与 OCR 文字框；标题栏点来自窗口边框属性。每次输入前重新核验窗口焦点和 PID。其他程序抢焦点、身份丢失或转发使进程退出会立即安全失败。不要在自动化期间手动移动鼠标、键盘或切换窗口；PyAutoGUI FAILSAFE 保留，移动到屏幕角落可中止后续动作并保留现场。

5.2.2 的单实例键使用 HOME（上游 krita/main.cc）；每轮私有 HOME 用来区分实例，保留 XAUTHORITY、DISPLAY、runtime、session bus 和系统资源路径。没有 --new-instance 假设，没有安装自动化插件。独立目录本身不算隔离成功，实际 PID/窗口仍须核验。仅支持 Ubuntu 原生 Krita，不假定 Snap/Flatpak/启动代理适用。已有 Krita 进程/窗口写入证据，绝不关闭它们。

## 命令和输出

完整安装与会话保持命令见 DEPLOY.md。

```bash
bash scripts/check_env.sh
bash scripts/run_gui.sh
bash scripts/report.sh outputs/实际UUID
bash scripts/test.sh
```

运行结果：outputs/<UUID>/metadata.json、automation.log、stages.jsonl、screenshots/、process.json、window.json、known-processes.json、summary.json、report.md。失败可另有 failure-windows.json；预检失败可能没有 GUI 截图，因为没有打开显示连接。素材候选与摘要会保留。输出目录从不覆盖旧轮次。

run/report 只有完整验证成功返回 0；失败返回 1；配置或检查不满足条件返回 2。失败和 NOT_RUN 轮次不标记有效。没有 memory.peak、有效内存峰值或目标峰值字段；本模块不是内存实验。

必须先取得一轮真实 COMPLETE/rc=0 和截图人工确认，才手动执行第二、第三轮；第一轮失败就先定位该阶段。三轮使用同一锁定 ORA，逐轮复核摘要。不能把单元测试或一次结果当作多轮稳定性通过。

## 文件与扩展边界

- config.py：严格配置与只读摘要；asset_inspector.py：原 ORA 元数据与唯一候选锁定。
- process_manager.py：原生进程启动及已见后代；window_manager.py：窗口/PID/可见性/最大化核验。
- visual_verifier.py：OCR、文字锚点、缩放/尺寸、截图差异与选择行验证。NumPy 仅作为 OpenCV 截图数组桥接，不处理 ORA 或分配实验负载。
- gui_controller.py：真实外部输入；state_machine.py：固定顺序、超时、逐阶段证据与失败保留。
- environment.py：X11、版本、依赖预检；report.py：报告独立验证；cli.py 和 scripts：入口。
- tests/：明确标记的模拟/临时截图逻辑测试，未生成真实 ORA，未操作真实 GUI。

未来监控应作为独立观察器读取本模块阶段时间和 PID 证据，不改变 GUI 动作顺序。当前不得为此加入 cgroup、内存限额或素材生成。
