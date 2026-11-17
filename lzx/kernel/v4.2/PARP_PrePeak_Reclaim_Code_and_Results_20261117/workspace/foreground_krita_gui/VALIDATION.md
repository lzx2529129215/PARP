# Qt 环境隔离修复验证

## 2026-11-01 本地 Ubuntu 22.04 虚拟机补充

真实环境为 Ubuntu 22.04.4 / Linux 6.17.13 / 原生 Krita 5.0.2 / 固定 1600×850 Xephyr/Openbox。完整功能轮 `dce9ea8f33804e77b9f007036397ecba` 已完成全部 16 阶段，returncode=0，无本轮窗口/已知后代残留，ORA SHA-256 不变。素材是独立新生成的 4096²×4 层功能 ORA，不是下文旧部署的 224 层素材。逐阶段截图、元数据位于相邻 `krita-vm-acceptance/gui-runs/functional`。

已修复：5.0.2 精确版本兼容、splash/main 绑定、xwininfo 客户区坐标（主窗口与对话框）、输入/截图一致的 X11 DISPLAY、OCR 缩放与坐标映射、千分位尺寸、同 PID 原生 popup 菜单范围、最大化重复核验。5.0.2 的实际保存提示为 Yes/No/Cancel；验收运行配置要求完整保存问题、Yes 和 Cancel，再鼠标点识别出的 No。OCR 置信度 55、窗口身份检查及失败保留仍保留。

Linux 单元测试 44 项通过，0 skip；真实 16 阶段结果独立于单元测试。大素材压缩与回收对比属于外部工具，最终结果以 `krita-vm-acceptance/ACCEPTANCE.md` 为准，不在此提前声明性能通过。

最终正式 48 层轮次全部完成 16 阶段、returncode=0、素材摘要不变：baseline-3/10bd7cfce8594223b2a6332a58fe6bae，apply-1/05239fbb6d7e488f8a53e535cb0c45e9，apply-2/47002b5c409742cb849d77f31db4c114，baseline-4/946f0efa48724bc29f910dd8b4fb1b9d。顺序为基线/apply/apply/基线，均使用同一锁定素材与测试条件。压缩场景实际产生 swap/zswap，四轮均无 OOM。

大素材验证增加两项有实测失败证据支持的识别修复：黄色内存状态栏取蓝通道读取精确尺寸；从已选中行的可见 Layer 文字定位该行，再单独 OCR Paint Layer 48。整侧栏图像差异仅 1.26% 时，改为对实际新图层行核对图像差异（实测 12.21%），保留 1.5% 差异阈值、蓝色选中、名称新增和文档修改标记。baseline-1/2 保留为失败轮，不计为成功。

以下为 2026-10-30 的历史验证，旧 Ubuntu 24.04/XFCE/224 层目标的 NOT_RUN 状态保持。

日期：2026-10-30，Asia/Shanghai。

## 故障证据与修复边界

用户提供的失败轮次 e91772c98c1d4616879a1f0dc5b4666e 中，Krita 的 xcb 插件路径指向 .venv/lib/python3.12/site-packages/cv2/qt/plugins。launch 原先直接继承父环境，未清理 Qt 路径，构成可确认的泄漏路径。未在此开发环境复现 Krita 崩溃，也未读取或修改目标 VM 的旧失败文件。

启动边界复制环境并移除三个 Qt 路径，版本预检使用相同逻辑；只接受原生 X11 平台，记录脱敏审计。PID/PPID/starttime、窗口/focus、鼠标键盘滚轮、OCR、素材摘要、Discard、截图、失败保留和完整正常退出验证均保留。startup-only 只运行前两个原有状态，不算完整通过。

## 本轮开发检查

|检查|结果|
|---|---|
|全部 unittest（原 26 项与新增 Qt/启动测试）|36 通过，0 失败，0 skip|
|路径污染模拟、父环境与四个会话变量保持|PASS：单元测试|
|清理环境传入真实 Python 子进程并核对值|PASS：普通非 GUI 子进程，不是 Krita|
|非法后端拒绝并留下审计、不创建进程|PASS：单元测试|
|版本预检使用清理环境，审计不含路径/会话原值|PASS：单元测试|
|startup-only 顺序、失败停止、不打开 ORA、不冒充完整通过|PASS：模拟状态机|
|全部 Python 源文件语法检查|PASS|
|全部 4 个 Shell 脚本 bash -n|PASS|
|ZIP CRC、FILELIST、SHA256SUMS、源码逐字节核对|PASS：见本轮发布摘要与 ZIP 旁 .verification.json|
|解压后完整 unittest / Python / Shell 检查|PASS：见本轮发布摘要与 ZIP 旁 .verification.json|

本轮日志在 outputs/qt-env-validation/。旧 outputs/development-validation 与已有 UUID 保留原样。发布包仅带明确标注的本轮开发摘要和日志，不含旧失败轮次或素材。

Qt 测试通过 mock Popen 检查 Krita 参数和传入环境，并用普通 Python 子进程验证环境跨进程传递。没有运行 Krita 或在目标 XFCE 验证实际 xcb 加载。图形输入、OCR、状态机仍是 mock/临时小 PNG 逻辑测试。

## NOT_RUN

- 目标 Ubuntu 24.04.4 / XFCE / X11 的 Krita 5.2.2 真实启动和 PID/窗口绑定。
- 用户指定的 4096×4096、224 层 ORA 真实存在、元数据及 SHA-256 核对。路径与 dd04dd…e09bf 是给定期望值。
- 完整 16 状态实际操作、Discard、进程真实正常退出 returncode=0。
- 三轮稳定性、任何内存曲线或峰值。

开发环境缺少 Krita、PyAutoGUI、OpenCV 和有效 X11，会话中也没有目标 VM 的可用 SSH 配置/凭据。本轮 check-env 会安全失败，不能声称故障已在真实 GUI 中消除。部署后先按 DEPLOY.md 运行 --startup-only；成功核对后才继续完整流程。

改动限于 foreground_krita_gui 和独立新版发布产物；旧 foreground_krita 的 75 文件按 SHA-256 冻结核对。未修改后台代码、桌面配置、旧部署、历史结果；本地 work 分支保持不变。依用户本轮明确授权，将修复及 ZIP 发布到 GitHub 独立分支，不合并覆盖 main。
