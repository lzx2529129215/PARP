# WPS 持续回放崩溃修复

本次修复针对实验回放中的 WPS，不改动系统安装和日常用户配置。原 WPS 应用 ID 15、长序列顺序、cgroup 归属及未来排序规则保持一致。

## 已定位的触发条件

安装版本为 `11.1.0.11723.XA`。其 `cfgs/oem.ini` 在 Product/Support 中设置 `EnableAccount=false`，但仍启动 `wpscloudsvr /qingbangong /start_from=qingipc autologin`。内核记录该进程在 `libqingbangong.so` 中段错误，先前的融合窗口还出现过 `libQt5CoreKso` 中段错误并退出139。

GDB 捕获到云服务主线程经 `AcceptHostMessage`、Qt信号分发进入 `libqingbangong.so` 后段错误。闭源库没有完整调试符号，尚不能断言内部具体对象或源码行；以下配置对照已定位可稳定触发/消除云服务错误的条件。

| 对照 | 应用模式 | 配置 | 结果 |
|---|---|---|---|
| 原安装 v1 | 融合窗口 | EnableAccount=false | 初始编辑器等待超时，2次云服务段错误 |
| 原安装 v1 | 原生组件 | EnableAccount=false | 9次重开可完成，但10次云服务段错误 |
| 独立副本 v7 | 原生组件 | EnableAccount=true | 3次重开通过，0次WPS段错误 |
| 同一独立副本 v8 | 原生组件 | 改回 EnableAccount=false | 3次重开完成，云服务再次出现4次段错误 |
| 修复副本 v10 | 原生组件 | EnableAccount=true | 三个完整WPS段共255条操作通过，0次WPS段错误 |

v7/v8 的可执行文件相同，切换同一副本的配置开关进行反向对照。测试无 memory.max 压力，关闭 bin；因此该配置触发的云服务错误不依赖 bin 排序。不能从这些数据推断修复后的内存性能一定提升。

[反向对照配置及二进制证据](../test/outputs/wps-reopen-20260914-v8/components/wps-config-evidence.json) · [GDB堆栈](../test/outputs/wps-reopen-20260914-v2/components/cloud-backtrace.txt) · [255条操作结果](../test/outputs/wps-reopen-20260914-v10/components/result.json)

## 实际修改

1. 新增 [wps_runtime.py](../test/test/wps_runtime.py)，每轮首次使用WPS时建立自己的运行副本：复制原版可执行文件/配置，引用系统只读库和资源；仅在副本中启用账号模块。没有执行账号登录，`EnableCloudDocs` 仍保持原设置。原/副本配置及可执行文件SHA-256写入 `wps-runtime/parp-runtime-manifest.json`。
2. 连续回放使用WPS自带的 `wps`、`wpp`、`et` 打开对应文档，避免依赖融合窗口嵌入时序。所有组件仍属于同一个WPS应用及其专用cgroup，文档重开不推进应用段游标。
3. 修正原生组件的“文件”菜单操作，并在另存为成功后更新当前文件名；后续操作能定位保存后的文档。
4. 默认应用提示优先于被遮挡的字体提示关闭。点击提示的关闭按钮，不点击会更改默认应用的确认按钮；不再同步等待被模态对话框阻塞的窗口激活。未关闭提示会明确报错，避免继续把按键发给提示框。
5. 保留进程崩溃/窗口丢失时终止回放的规则，不通过自动重试、跳过原始行或复制文件伪造成功。

原融合窗口在配置修正后未见段错误，但仍出现过“编辑器嵌入等待超时”，因此最终采用“配置修正 + 原生组件”组合，而非宣称已修补WPS闭源二进制的所有缺陷。

## 专项验证

- 自动化测试31项通过：WPS运行副本/模态提示3项、排序/控制19项、原回放9项。
- v10执行了原表三个完整WPS段的255条操作，覆盖原始第694行，并完成三次另存为文件内容校验。内核日志未记录WPS段错误。
- 这些是WPS专项回归，不等于完整85段压力对照通过；原始压力实验的失败证据保留。

```bash
cd /home/lzx/Desktop/PARP
python3 test/test/wps_reopen_probe.py --modes components --fixed-runtime \
  --operations --cycles 3 --output-dir test/outputs/wps-regression-new
python3 test/test/user_events_replay.py --continuous run \
  --output-dir test/outputs/oracle-fixed-new-pair
```

完整回放入口会自动使用本修复，两轮使用相同修复策略。输出目录需要是新目录。

## 持续回放集成验证

[前20段双轮报告](../test/outputs/oracle-wps-fixed-20260914-v1/REPORT.md) · [验收明细](wps-crash-fix-acceptance.json) · [修复后排序Excel](../test/outputs/oracle-wps-fixed-20260914-v1/oracle/switch-rankings.xlsx)

- native 与 oracle 两轮均 SMOKE_PASS，各完成20段、609条操作；两轮原始第694行及后续保存均通过。
- oracle 完成372批真实内核提交/读回校验，包含WPS组件重开后的cgroup绑定更新。
- 两轮检查时间窗内未记录WPS段错误；原安装OEM配置哈希均保持不变。
- 常驻服务已恢复active、原内核控制值匹配、本次应用单元全部停止，恢复无错误。
- 这是无压力的20段集成回归，加上255条WPS专项验证；不能替代完整85段受压性能对照，也不据此声称bin性能改善。
