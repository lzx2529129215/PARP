# WPS 导入场景说明

本目录中的 `wps_*.json` 从 `huawei_mem/wzx/automation-wps/configs/automation/` 原样导入，仅在 `lzx` 侧使用；未修改 `wzx` 的任何源码或场景文件。

场景依赖本项目根目录下的 `samples/wps/` 测试素材，例如 `word_200m.docx`、`presentation_200m.pptx`、`spreadsheet_200m.xlsx`、测试图片和视频。涉及模板、插入媒体、字体栏位等固定坐标的步骤，应先在当前桌面分辨率与 WPS 版本下校准；缺少素材时，`assert_file` 会在执行前停止场景。

推荐先执行无副作用的启动检查：

```bash
python3 automation/app_automation.py configs/automation/wps_smoke_test.json \
  --session-id wps_smoke_$(date +%Y%m%d_%H%M%S) \
  --test-slice huawei-test.slice
```

执行 Writer、PPT、Excel 性能场景前，请确认保存路径指向本轮实验输出目录，避免覆盖原始测试文档。
