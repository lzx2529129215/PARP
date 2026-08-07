# Repository Phase 0基线

本基线冻结`/home/lzx/Desktop/huawei/huawei_mem/lzx`在Git HEAD `d11f626d7d33ceab2b82c1906b0959a204d0d55c`上的职责、入口、模型、内核、关键数据、保护资产和安全测试状态。来源审计为`outputs/repository_audit_20260717_175447`。

## 子系统

共登记11个一级子系统：应用预测、Runtime Monitor、Automation、Harmony/Linux内存采集、MGLRU内核、cache_ext、共享配置、设计/项目文档、outputs和pytest缓存。

## 正式入口

- `runtime_monitor/monitor.py`：CANONICAL。
- `runtime_monitor/online_monitor.py`：COMPATIBILITY。
- `automation/run_automation.sh`：WRAPPER。
- `automation/app_automation.py`：CANONICAL实现。

## 模型和内核

登记5个checkpoint；正式在线模型是`operation_predictor/outputs/checkpoints/app_lstm_duration/lsapp_app_lstm_duration_switch.pt`。当前内核为DAMON observe变体，明确回退为`/boot/vmlinuz.old`指向的bindfix observe内核。

## 关键资产和测试

登记14项数据/实验资产和14项保护资产。本次79项host测试、198个Python、60个Shell和62个JSON检查通过；设备、GUI、内核运行和训练未执行。

## 后续权限

基线已冻结，但14项问题仍为OPEN。当前不允许移动outputs、归档备份、统一配置、重构源码、调整内核布局或删除文件。`safe_to_start_phase_1=false`，需用户先审核Phase 1候选边界。

## 验证

运行`python3 outputs/repository_phase0_20260717_195813/raw/evidence/validate_phase0.py`，再在Phase 0输出目录执行`sha256sum -c validation/phase0_manifest.sha256`。
