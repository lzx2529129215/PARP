# LSAPP-expanded 在线场景

这套场景用于解决“训练应用集合与真实自动化应用集合不一致”的问题。它不改 `/home/lzx/Desktop/PARP/test`，运行入口、场景生成器和结果分析器都位于 `runtime_monitor`。扩展入口默认使用独立的 Xvfb＋Openbox，避免宿主 GNOME 锁屏或重认证终止实验；旧 Test2 入口仍默认使用 Xephyr。 <!-- lzx-note -->

## 应用集合

在原有 Firefox 功能域、LibreOffice、VLC、GIMP、Audacity、Thunderbird、Evince、Files、Calculator 九个免登录应用上，新增六个已安装应用。浏览器窗口由原生 Debian 包 GNOME Web（Epiphany）执行，并继续映射到模型中的 `Firefox` 浏览器功能域；Ubuntu 的 Snap Firefox 因私有 `/tmp` 无法连接隔离 Xephyr。 <!-- lzx-note -->

- Calendar：本地日历视图；
- Rhythmbox：本地 WAV 音乐播放；
- Image Viewer（EOG）：本地图片查看；
- Shotwell：本地图库/照片查看；
- System Monitor：本机资源页面；
- AisleRiot Solitaire：本地纸牌操作。

聊天和社交客户端仍不纳入评分集合，因为账号登录会使自动化无法从干净状态复现。

对应文件：

- Runtime scope：`config/runtime_app_scope.lsapp_expanded.json`；
- LSAPP 映射：`../operation_predictor/data/lsapp_expanded/mapping/lsapp_to_linux.json`；
- 训练入口：`../operation_predictor/v3/scripts/run_lsapp_expanded_pipeline.sh`；
- 场景生成器：`scripts/build_lsapp_expanded_scenario.py`；
- 在线运行入口：`scripts/run_lsapp_expanded_online.sh`；
- 在线准确率分析：`scripts/analyze_lsapp_expanded_online.py`。

## 场景约束

场景不会按固定的 15 应用轮询顺序运行。生成器从 LSTM `test.csv` 中选择连续的真实前台切换块，贪心覆盖 15 个目标应用，并保存原始 session、时间戳、当前应用、下一应用和停留时间分桶。不同 LSAPP session 之间以 `LSAPP_BLOCK_START` 分隔，不计跨块预测。

原始停留时间只保留分桶关系，压缩为 0.8/1.2/1.8/2.5 秒，以便自动化可执行。在线分析只接受“切换前、且 foreground 仍等于 current App”的预测，切换后才产生的目标 App 预测不会被当作命中。

所有输入均为本地生成的 HTML、TXT、EML、WAV、PPM 和 PDF，不访问网络，也不依赖用户账号。

## 运行

先训练 15 应用模型：

```bash
cd /home/lzx/Desktop/PARP/lzx/tool/operation_predictor
bash v3/scripts/run_lsapp_expanded_pipeline.sh
```

运行一次 60 条以上 held-out 转换的在线采集：

```bash
cd /home/lzx/Desktop/PARP
bash lzx/service/runtime_monitor/scripts/run_lsapp_expanded_online.sh \
  --transitions 60 --seed 20260814 --bridge-mode shadow-write
```

输出位于 `lzx/service/outputs/runtime_monitor/<session-id>/`，重点检查：

- `config/lsapp-expanded-heldout.coverage.json`：数据来源、哈希、块边界与应用覆盖；
- `model/online_lstm_predictions.csv`：逐次在线预测；
- `review/lsapp-expanded-online-lstm.json`：Top-1/3/5、宏平均和覆盖率；
- `review/test3_memory_shadow_report.json`：预测后的只读内存观测；
- `parp/parp_bridge_events.csv`：AppLSTM prior 写入与快照确认。

在线 Top-K 只证明预测链路与真实窗口序列的匹配程度。PageFault、OOM 和 effective-tier/Tier2 的收益仍需独立的同环境 OFF/Apply 配对实验，不能由此报告替代。
