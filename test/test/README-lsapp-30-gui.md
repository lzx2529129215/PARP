# LSApp 30 应用：新增应用 GUI 验收

入口沿用 `test/test` 的 Xvfb/Openbox 和 X11 窗口读取能力。`lsapp_30_gui.py` 提供私有桌面、独立 profile/session bus、进程归属检查、按键鼠标操作、JSON 动作回放和退出检查。这里不调用预测器或内核概率写入接口。

本次结果在 `test/outputs/lsapp-30-gui-20260910-v1`：新增 15 个应用全部通过基本操作、焦点切回、进程归属和退出清理检查。原有 15 个应用未在本轮重测。完整 30 应用预测、TTL/bin、五场景成对性能回放不属于这次 GUI smoke 验收。

## 文件

- `gui-acceptance.json`：每个应用的结果与进程数。
- `progress.json`：当前阶段。
- `APP/operation-review.json`、`APP/process-audit.json`、`APP/cleanup.json`：操作审阅、进程归属、退出证据。
- `APP/actions.jsonl` 与根目录 PNG/JSON：操作前后窗口、焦点、cgroup 和截图。
- `action-plan.json`：完整探索记录，含首次设置和重试，**不能直接充当 Native/PARP 的公平场景计划**。
- `APP/replay-plan.json`：聊天动作片段，需满足文件中的初始窗口条件；Gajim 已实际运行回放并验证本地服务收到新消息。其他片段仍需在场景集成时逐一复核。
- `prepared-profiles.tar.gz`：已配置的本地测试资料，邮件已统一到每款 50 封。配置含本次输出目录的绝对路径；更换目录需要重新准备或替换配置中的路径。
- `test/configs/lsapp_30/runtime_app_scope.json`：新增实际观测到的 WM_CLASS/程序名称，保留旧 15 个 ID；独立保存，未切换常驻服务。

## 使用已准备的测试目录

从仓库根目录运行，先创建私有桌面：

```bash
TASK_OUT=/home/lzx/Desktop/PARP/test/outputs/lsapp-30-gui-20260910-v1
systemd-run --user --unit=parp30-gui-manual --collect \
  /usr/bin/python3 /home/lzx/Desktop/PARP/test/test/lsapp_30_gui.py desktop --output-dir "$TASK_OUT"
python3 test/test/lsapp_30_gui.py start --output-dir "$TASK_OUT" --app FALKON -- \
  falkon "file://$TASK_OUT/fixtures/browser.html"
python3 test/test/lsapp_30_gui.py focus --output-dir "$TASK_OUT" --app FALKON
python3 test/test/lsapp_30_gui.py input --output-dir "$TASK_OUT" --app FALKON --label scroll -- key Page_Down
python3 test/test/lsapp_30_gui.py audit --output-dir "$TASK_OUT" --app FALKON
python3 test/test/lsapp_30_gui.py stop --output-dir "$TASK_OUT" --app FALKON
systemctl --user stop parp30-gui-manual.service
```

其他应用的实际启动参数见 `APP/launch.json` 或 automation manifest。`start` 会拒绝已经运行的相同测试实例，清除它上一轮遗留的私有 runtime socket，并等待窗口出现；`input` 在发出按键前检查前台窗口归属。`stop` 检查子进程是否残留。窗口出现只表示可以开始验收，不自动表示业务操作通过。

聊天服务使用 `lsapp_30_local_xmpp.py server/echo --output-dir "$TASK_OUT/xmpp"`，分别放入独立的用户 transient service；服务器只监听 `127.0.0.1:5222`。账号和密码均为合成资料。`prepare` 用于新目录初始化，不应在已运行的服务器上执行。证书有效期 7 天，过期后应停止服务并重新生成测试证书。Gajim/Dino/Psi+/Kaidan 启动时使用 `--trusted-cert "$TASK_OUT/xmpp/localhost.crt"`，在私有 mount namespace 内增加证书，随后降回原用户；不会修改宿主证书信任。

```bash
python3 test/test/lsapp_30_gui.py replay --output-dir "$TASK_OUT" --app GAJIM \
  --plan "$TASK_OUT/GAJIM/replay-plan.json"
```

回放前须启动应用和本地消息服务，并满足 plan 的窗口条件。聊天回放检查本次调用之后新增的服务器日志，不能用旧的同名消息冒充新结果。屏幕布局变化会使坐标操作失效，因此应固定桌面尺寸、窗口位置和初始资料；正式场景还要加入应用内容完成条件。

## 已处理的环境问题

- Dino 的可执行文件为 `dino-im`。
- GNOME Software 使用已安装程序的 AppStream 元数据构成本地目录，目录加入私有 `XDG_DATA_DIRS`；测试 profile 关闭自动下载及更新。首次探索曾触发更新下载，apt/dpkg 日志没有额外升级记录；它的缓存和 PackageKit 服务不能计入某个预测目标的独占内存。
- 聊天客户端依赖的证书、密钥环及配置均位于独立测试资料中。
- Claws Mail 重新启动前清理私有 runtime 中的遗留 socket；系统其他应用的 socket 不受影响。
- 对话框或应用异常退出不判操作成功；截图和服务器/邮件内容必须另行核验。

本轮没有训练新模型、部署新词表或进行内存回收性能实验。下一阶段生成 30 应用数据时仍须按原始身份维护打开状态、保留未知应用边界，并使用新的 checkpoint。
