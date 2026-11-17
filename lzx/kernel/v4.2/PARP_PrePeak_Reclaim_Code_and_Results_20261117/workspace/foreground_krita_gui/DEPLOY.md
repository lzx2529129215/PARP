# Ubuntu 24.04.4 / XFCE / X11：Qt 环境修复部署

2026-11-01 补充：本地 qi 虚拟机实际为 Ubuntu 22.04.4 / Linux 6.17.13 / Krita 5.0.2，使用相邻 `krita-vm-acceptance` 工具与固定 Xephyr X11 运行配置。该配置验证完整保存问题和 Yes/Cancel 后点击 No；不能直接套用下方历史 5.2.2 的 Discard 标签或 224 层素材路径。当前代码和真实结果见 `VALIDATION.md` 及相邻 `krita-vm-acceptance/ACCEPTANCE.md`。下方 ZIP 名称、旧环境和素材期望值均为历史部署记录，不是本轮运行命令。

使用新版 foreground_krita_gui_qt_env_fix_bundle.zip，不要使用旧 foreground_krita_gui_bundle.zip。在全新目录解压，保留所有旧部署和 outputs/e91772c98c1d4616879a1f0dc5b4666e。包内没有 ORA、虚拟环境或旧插件模块。

```bash
sha256sum -c foreground_krita_gui_qt_env_fix_bundle.zip.sha256
DEPLOY_DIR=$(mktemp -d "$HOME/oom-stage-a/oom-krita-gui-qtfix.XXXXXX")
unzip -n foreground_krita_gui_qt_env_fix_bundle.zip -d "$DEPLOY_DIR"
cd "$DEPLOY_DIR"
sha256sum -c foreground_krita_gui/SHA256SUMS
cd foreground_krita_gui
```

在普通 ubuntu 桌面用户下安装本轮独立依赖：

```bash
sudo apt-get update
sudo apt-get install python3-venv python3-tk python3-dev scrot wmctrl xdotool x11-utils tesseract-ocr tesseract-ocr-eng libgl1 libglib2.0-0 tmux
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export KRITA_GUI_PYTHON="$PWD/.venv/bin/python"
export DISPLAY=:12.0
xdpyinfo >/dev/null
bash scripts/test.sh
bash scripts/check_env.sh
```

保留当前桌面会话的 XAUTHORITY、XDG_RUNTIME_DIR、DBUS_SESSION_BUS_ADDRESS，不从其他会话复制，不使用 sudo 启动 GUI、xhost +、offscreen。如果 QT_QPA_PLATFORM 原来设为非 X11，可对本终端单条命令使用 `env QT_QPA_PLATFORM=xcb bash scripts/run_gui.sh ...`；不修改全局或桌面配置。其他 Qt 路径污染由代码在子进程副本中清理，无需全局 unset。Krita 使用 Ubuntu 原生 5.2.2。

默认英语 OCR 标签、相对区域及蓝色选择行判据未在目标桌面校准；中文 UI 须先安装相应 Tesseract 语言包并配置标签。

锁定用户指定的原素材，先检查实际元数据和摘要。以下只读取 ORA，以独占方式创建新配置，拒绝覆盖已有文件：

```bash
"$KRITA_GUI_PYTHON" - <<'PY'
import json
from pathlib import Path
from oomlab_krita_gui.asset_inspector import inspect
from oomlab_krita_gui.config import load, validate
asset_path = '/home/ubuntu/oom-stage-a/krita_gimp_lab/assets/krita_14gib_4096x4096_224layers.ora'
expected = 'dd04ddf49d28690527dbc85d879981af3ea9ed62f1a3ce37053e453fef6e09bf'
asset = inspect(asset_path)
if (asset['sha256'], asset['width'], asset['height'], asset['layers']) != (expected, 4096, 4096, 224):
    raise SystemExit('原素材元数据或 SHA-256 不匹配，停止')
config = load('configs/gui.json')
config['asset'] = dict(path=asset['path'], sha256=expected)
validate(config)
with Path('configs/gui.vm.json').open('x') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
    f.write('\n')
print(json.dumps({k: asset[k] for k in ['path', 'sha256', 'width', 'height', 'layers']}, indent=2))
PY
```

上述尺寸、层数、摘要是用户提供的期望值，本开发环境没有该文件，未实测核对。

可以使用独立 tmux 服务器继承当前授权会话，保持 XFCE/X11 桌面存活：

```bash
tmux -L oom-krita-gui-qtfix new-session -s gui
```

在 tmux 内确认当前目录、KRITA_GUI_PYTHON、DISPLAY 和 xdpyinfo，再先只验证启动及实例绑定：

```bash
bash scripts/run_gui.sh --config configs/gui.vm.json --startup-only
```

首轮应返回 0 并打印新 UUID。检查对应 process.json、window.json、qt-environment.json、stages.jsonl、screenshots/、summary.json：

- Qt 清理前可能存在三个路径变量；清理后均 present=false。
- session_unchanged 全部 true，platform_allowed=true。
- /proc PID、PPID、starttime、原生 exe、_NET_WM_PID、窗口 ID 和活动焦点通过。
- status=STARTUP_VERIFIED、startup_test=PASS；完整流程 valid=false、gui_test=NOT_RUN 是预期结果，尚未验证正常退出。

失败立即停止，不继续完整流程；保留日志、截图和现场，不关闭无关窗口。成功后人工按 PID 核对并正常关闭本轮空白窗口，不操作其他实例。代码不强杀进程。

只有真实启动绑定通过、原素材核对完成后，手动继续完整 GUI 流程：

```bash
bash scripts/run_gui.sh --config configs/gui.vm.json
bash scripts/report.sh outputs/实际完整流程UUID
```

完整流程重新创建并验证自己的实例，仍执行全部 16 状态。只有 COMPLETE、valid=true、VERIFY_EXIT.returncode=0 且人工检查截图，才可认可完整真实操作。仅启动验证或 mock 测试不算完整通过。首次失败不自动连跑三轮。

运行期间不要操作鼠标键盘或抢焦点；保留 PyAutoGUI FAILSAFE。tmux 脱离 SSH 不等于无界面；桌面必须存活。没有新增内存限额、素材生成、cgroup 或峰值采集。

复现构建（输出必须不存在）：

```bash
"$KRITA_GUI_PYTHON" scripts/build_bundle.py --output ../foreground_krita_gui_qt_env_fix_rebuilt.zip
```

FILELIST.txt 和 SHA256SUMS 对应该 ZIP。缓存、.venv、旧 ZIP、历史轮次不混入；不要用旧 ZIP 覆盖新源码。
