# Workload-Aware 内存访问识别与下一阶段预测原型

本目录是 PARP 的独立原型，严格不修改仓库其他目录。它将已有的区域/cgroup观测思路转换成多维 Workload 状态，并在用户态生成 1-5 秒范围内的 OBSERVE/SHADOW 预测快照。

## 当前边界

- 不复制 Linux 内核、Runtime Monitor 或 App-LSTM。
- 不读取或写入 `/sys/kernel/debug`、`/dev/myfs`、ioctl 或 cgroup 控制文件。
- 不执行预取、回收、压缩、迁移、swap 或 APPLY。
- 缺少区域顺序时，`AccessOrder` 必须为 `UNKNOWN`；低置信度时使用 Native fallback。
- App-LSTM 与 Workload Predictor 完全独立：前者预测 App，后者预测访问行为/阶段。

## 运行

在仓库根目录执行：

```powershell
python lzx/lzx-zr/tools/run_workload_aware.py `
  --input lzx/lzx-zr/tests/fixtures/observations.jsonl `
  --output-dir lzx/lzx-zr/outputs/features `
  --mode SHADOW
```

输出包括 `features.jsonl`、`predictions.jsonl`、`prediction_snapshot.json` 和 `parp_shadow_hint.json`。最后一个文件只是兼容 PARP snapshot 概念的审计提示，不会下沉到内核。

## 测试

```powershell
python -m unittest discover -s lzx/lzx-zr/tests -p "test_*.py" -v
```

详细边界见 `docs/00_repository_audit.md`，设计、schema、适配器和验收计划分别见 `docs/01_workload_design.md` 至 `docs/04_acceptance_report.md`。
