# Vocab 配置说明

第一版暂不迁移 operation predictor 的 vocab JSON，避免影响训练脚本和 checkpoint 加载路径。

当前 vocab 仍保留在：

```text
operation_predictor/data/vocab/
```

后续如果迁移 vocab，需要同步检查训练、评估、推理脚本中的 `--app-vocab`、`--group-vocab`、checkpoint metadata 和 README 示例路径。
