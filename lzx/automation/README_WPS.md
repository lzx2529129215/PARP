# lzx/automation 中的 WPS 自动化

本文档描述已合并到 `huawei_mem/lzx` 的 WPS 自动化逻辑：

- `automation/app_automation.py`：JSON 场景执行器。
- `automation/run_automation.sh`：图形环境、systemd cgroup 和 trace 启动包装器。
- `automation/run_wps_case.sh`：Excel 测试用例中 7 个 WPS 场景的统一入口。
- `configs/automation/*.json`：自动化场景。

执行器不依赖固定用户主目录；项目根目录由脚本所在的 `lzx/automation`
自动推导，场景位于 `lzx/configs/automation`，样本位于 `lzx/samples/wps`。

## 1. 项目目录

合并后的结构：

```text
lzx/
├── automation/
│   ├── app_automation.py
│   ├── run_automation.sh
│   └── run_wps_case.sh
├── configs/
│   └── automation/
│       ├── wps_perf_0010_templates.json
│       ├── wps_perf_0020_new_documents.json
│       ├── wps_perf_0030_ten_documents.json
│       ├── wps_perf_0040_word.json
│       ├── wps_perf_0050_presentation.json
│       ├── wps_perf_0060_spreadsheet.json
│       └── wps_perf_0070_pdf.json
├── samples/wps/
└── outputs/wps/
```

## 2. 安装依赖

```bash
sudo apt update
sudo apt install -y python3 xdotool wmctrl xclip x11-utils
```

确认 WPS 命令：

```bash
command -v wps
command -v wpp
command -v et
command -v wpspdf
```

授予脚本执行权限：

```bash
cd /home/lzxxxxxx/桌面/huawei/huawei_mem/lzx
chmod +x automation/run_automation.sh automation/run_wps_case.sh automation/test.sh
```

不要使用 `sudo` 运行自动化脚本，否则 `$HOME`、DISPLAY 和桌面权限可能变成 root 用户的环境。

## 3. X11/Wayland 要求

自动化使用 `xdotool`，需要 WPS 运行在 X11 或 Xwayland：

```bash
echo "$XDG_SESSION_TYPE"
echo "$DISPLAY"
```

如果 `xdotool` 找不到 WPS 窗口，建议注销 Ubuntu，在登录界面的齿轮菜单中选择 **Ubuntu on Xorg** 后重新登录。

窗口检测：

```bash
xdotool search --onlyvisible --class 'wps|wpp|et|wpspdf'
```

## 4. 七个 WPS 场景

| 用例 | 场景文件 | 内容 |
| --- | --- | --- |
| `0010` | `wps_perf_0010_templates.json` | 稻壳模板浏览、上下滑动和可选下载 |
| `0020` | `wps_perf_0020_new_documents.json` | 新建并保存 Word、PPT、Excel |
| `0030` | `wps_perf_0030_ten_documents.json` | 打开 10 个大文档并切换标签页 |
| `0040` | `wps_perf_0040_word.json` | Word 复制粘贴、图片、网页、查找、保存和另存为 |
| `0050` | `wps_perf_0050_presentation.json` | PPT 放映、页面复制、图片、视频、形状和删除页面 |
| `0060` | `wps_perf_0060_spreadsheet.json` | Excel 过滤、排序、列、公式、Sheet、字体和首尾跳转 |
| `0070` | `wps_perf_0070_pdf.json` | PDF 翻页、首尾跳转、最小化和最大化 |

查看统一入口帮助：

```bash
cd /home/lzxxxxxx/桌面/huawei/huawei_mem/lzx
automation/run_wps_case.sh --help
```

## 5. 先执行 dry-run

dry-run 只检查场景和输出动作，不会启动 WPS，也不会要求样本文件实际存在：

```bash
cd /home/lzxxxxxx/桌面/huawei/huawei_mem/lzx
automation/run_wps_case.sh 0010 --dry-run
automation/run_wps_case.sh 0040 --dry-run
```

## 6. 准备样本文件

默认样本目录是：

```bash
mkdir -p "samples/wps"
mkdir -p "outputs/wps"
```

样本文件可以使用任意路径。运行时用 `--var NAME=VALUE` 覆盖，不需要修改 JSON。

Word 用例示例：

```bash
cd /home/lzxxxxxx/桌面/huawei/huawei_mem/lzx
automation/run_wps_case.sh 0040 \
  --var WPS_WORD_FILE="$HOME/wps-samples/word_200m.docx" \
  --var WPS_IMAGE_FILE="$HOME/wps-samples/image_1m.png" \
  --var WPS_WORD_SEARCH_KEY="测试"
```

PPT 用例示例：

```bash
automation/run_wps_case.sh 0050 \
  --var WPS_PPT_FILE="$HOME/wps-samples/presentation_200m.pptx" \
  --var WPS_IMAGE_FILE="$HOME/wps-samples/image_1m.png" \
  --var WPS_VIDEO_FILE="$HOME/wps-samples/video_10m.mp4"
```

Excel 用例示例：

```bash
automation/run_wps_case.sh 0060 \
  --var WPS_EXCEL_FILE="$HOME/wps-samples/spreadsheet_200m.xlsx" \
  --var EXCEL_NUMERIC_HEADER="B1" \
  --var EXCEL_TEXT_HEADER="C1" \
  --var EXCEL_FILTER_KEYWORD="key" \
  --var EXCEL_FORMULA_RANGE="K2:K50000"
```

PDF 用例示例：

```bash
automation/run_wps_case.sh 0070 \
  --var WPS_PDF_FILE="$HOME/wps-samples/document_200m.pdf"
```

10 文档用例可以先导出环境变量：

```bash
export WPS_DOC_01="$HOME/wps-samples/01.docx"
export WPS_DOC_02="$HOME/wps-samples/02.pptx"
export WPS_DOC_03="$HOME/wps-samples/03.xlsx"
# 按相同方式设置 WPS_DOC_04 到 WPS_DOC_10

automation/run_wps_case.sh 0030
```

环境变量和 `--var` 都可以覆盖 JSON 中的默认值；`--var` 优先级最高。

## 7. 输出 trace

```bash
SESSION_ID="wps_word_$(date +%Y%m%d_%H%M%S)"
mkdir -p "outputs/runtime_monitor/$SESSION_ID/model"

automation/run_wps_case.sh 0040 \
  --var WPS_WORD_FILE="$HOME/wps-samples/word_200m.docx" \
  --var WPS_IMAGE_FILE="$HOME/wps-samples/image_1m.png" \
  --session-id "$SESSION_ID" \
  --trace-output "outputs/runtime_monitor/$SESSION_ID/model/automation_trace.csv"
```

## 8. 坐标校准

以下界面会随 WPS 版本、主题、语言、屏幕分辨率和缩放比例变化：

- 稻壳模板分类和下载按钮。
- PPT 的插入视频、形状按钮。
- Excel 字体框和筛选菜单快捷键。

场景使用窗口相对比例，例如 `PPT_VIDEO_BUTTON_X=0.73` 表示窗口宽度的 73%。可以在命令行校准：

```bash
automation/run_wps_case.sh 0050 \
  --var WPS_PPT_FILE="$HOME/wps-samples/sample.pptx" \
  --var WPS_IMAGE_FILE="$HOME/wps-samples/image.png" \
  --var WPS_VIDEO_FILE="$HOME/wps-samples/video.mp4" \
  --var PPT_VIDEO_BUTTON_X=0.70 \
  --var PPT_VIDEO_BUTTON_Y=0.14
```

获取当前窗口和鼠标信息：

```bash
xdotool getactivewindow getwindowgeometry --shell
xdotool getmouselocation --shell
```

建议先将 VMware Ubuntu 分辨率固定为 `1920×1080`、显示缩放固定为 `100%`，再校准一次坐标。

## 9. 常见问题

`Can't open display`：请在已登录 Ubuntu 图形桌面的终端运行，也可以尝试：

```bash
automation/run_wps_case.sh 0040 --display :0
```

中文无法由 `xdotool type` 输入：新执行器通过 `xclip` 写剪贴板再粘贴，确保已经安装 `xclip`。

找不到样本文件：错误会显示缺失的完整路径，使用 `--var` 传入真实路径。

按钮点偏：保持窗口最大化并调整相应的 `*_X`、`*_Y` 变量。

Excel 筛选菜单不响应：中文和英文 WPS 的菜单访问键可能不同，请覆盖 `EXCEL_FILTER_NUMBER_MENU_KEY`、`EXCEL_FILTER_GREATER_EQUAL_KEY`、`EXCEL_FILTER_TEXT_MENU_KEY` 和 `EXCEL_FILTER_CONTAINS_KEY`。
