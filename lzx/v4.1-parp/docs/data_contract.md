# v4.1 数据契约

## samples.csv

每行是一次 App 预测/内存决策时刻：

```text
sample_id,timestamp,current_app_id,current_app,actual_next_app_id,actual_next_app,available_pages,base_headroom_pages,burst_pages
```

`actual_next_app_id` 只用于离线评估，不能作为在线输入。

## app_states.csv

每行是某个 sample 下的一个 App：

```text
sample_id,app_id,app,domain_id,running,foreground,reclaimable_pages,launch_pages
```

`launch_pages` 是该 App 尚未运行时预计新增的工作集；`reclaimable_pages` 只描述当前已运行 App 可承担的回收规模。

## predictions.csv

每行是 LSTM 的一个候选输出：

```text
sample_id,horizon_ms,rank,app_id,app,raw_score,use_score,score_mode,model_version,ttl_ms
```

`softmax` 分数保留模型输出质量；`sigmoid` 分数因 v2 使用 BCE 训练，属于未校准分数，v4.1 会在候选集合内归一化后再用于 headroom 反事实计算。

`model_version`、`horizon_ms` 和 `ttl_ms` 是控制面审计字段。任何版本或有效期不匹配，都应在真实内核接入时回退 Native。
