# PARP Prediction Contract Study

2026-11-19。先用脚本理想输出验证完整“应用间→应用内→提前回收”链路，再据此确定训练目标及缺失采集。未训练真实预测模型，未上传GitHub。

- `OUTPUT_CONTRACT.json`：最终机器可读接口，**保留缺页预测**。默认从当前时刻起未来20秒、40个0.5秒窗口，每0.5秒更新。
- `预测接口与代码改动.md`：各字段、为何需要、high边界、三项回收改进。
- `实验结果.md`、`results/VERIFIED.json`：37项真实检查、原始批次及适用范围。
- `交给采集人员的字段与步骤.md`：已有数据怎么复用、缺什么、怎么采、当前VM支持哪些接口。
- `prediction_contract.py`、`study_pipeline.py`、`scheduler_fix.py`：理想提供器、输出消费和候选修正。
- `controller/`、`source_baseline.json`：上传版控制器原文件，15项哈希一致。候选修改通过外层/子类接入，保留原版对照。
- `results/source_archives/`：逐批实际执行代码，与各批PLAN哈希匹配。
- `run_native.py`、`memory_worker.py`、`native_support.py`：真实cgroup负载、校准、计时与采集。
- `run_simulation.py`、`test_contract.py`：逻辑筛查、回归和格式检查；不把它们当原生结果。
- `stage_native.py`、`prepare_cgroup.py`、`vm_service.py`：本地副本运行、固定隔离组设置及普通用户修正通道；root父进程只等待和清理，不接受执行命令。
- `analyze_results.py`：重新审计原始结果和源码，只有证据匹配才生成VERIFIED。

迁移后的VM：`D:\Virtual Machines\Ubuntu 64 位\Ubuntu 64 位.vmx`。本次已恢复运行时共享，新的Ubuntu入口为 `/home/qi/parp-code-share-20261119`。

重跑完整批次：

```bash
bash /home/qi/parp-code-share-20261119/PARP_Prediction_Contract_Study_20261119/start_contract_study.sh
```

先执行校验和逻辑检查，再本地sudo设置隔离组，普通用户运行校准和25项完整批次。控制目录用于同一授权任务内纠错重测；完成后写 `control/STOP.json` 让普通用户通道结束，root父进程清理自己的隔离组。无网络监听。

宿主机可运行 `python analyze_results.py` 复核证据。旧实验和旧PPT未修改。本批是整应用干净冷文件页与空间/申请耗时的理想验证，不替代匿名压缩、页级热页保护或真实GUI性能测试。
