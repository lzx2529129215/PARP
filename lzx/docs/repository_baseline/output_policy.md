# 输出政策

未来标准结构：

```text
outputs/<subsystem>/<session_id>/
├── metadata/
├── raw/
├── derived/
├── reports/
├── logs/
└── validation/
```

- raw不可修改；derived必须可由raw和配置重建；reports由derived生成。
- logs不属于正式Schema；validation保存命令、版本和SHA。
- 每个session必须有metadata；模型输出必须绑定checkpoint和vocab。
- 设备输出记录device ID、系统版本和内核release。
- 内核实验记录source/config/release、policy mode、apply状态和回退内核。
- 当前阶段不迁移任何历史output。
