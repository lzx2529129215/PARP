# 与真实操作驱动对接

以下数值仅演示格式。实际调用必须替换当前 RSS、应用上下文并读取返回的 generation。

先发一行：

```json
{"event":"forecast","app_name":"gimp","sequence_num":5,"current_op_id":1,"pressure_gib":8,"rss_bytes":1073741824}
```

若返回 `generation=1`，在第一个预测操作实际开始时发：

```json
{"event":"enter","generation":1,"step":0}
```

第二、三、四个操作实际开始时分别发 step=1、2、3；四个预测阶段结束时发 step=4。不能用固定等待代替操作边界，也不能把五个 enter 事件同时发出。

提前结束发：

```json
{"event":"stop"}
```

驱动在每行后刷新标准输入，并持续读取标准输出，避免管道堵塞；没有 `--apply` 时只输出候选，不写 cgroup。关闭标准输入会结束控制器，high 在正常退出路径恢复。
