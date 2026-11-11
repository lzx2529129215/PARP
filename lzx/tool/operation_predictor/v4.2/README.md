# 当前应用间预测与复用融合

当前常驻服务采用 RankProp v4.2，由 v4.1 重入时序模型和驻留页复用融合组成，模型接口版本为 402。

## 代码入口

- [OnlineRankProp41Predictor.predict_bundle](../../../service/runtime_monitor/predictor_rankprop41.py) 根据前台切换历史、真实打开的应用集合和时间戳计算时序预测。
- [ResidentReuseController.fuse](../../../service/runtime_monitor/core/resident_reuse.py) 融合时序预测、file/anon 旧驻留页复用估计、采样覆盖率和近期活动，生成 Q15 保护分数、唯一排序及 memory.low 目标。
- [temporal_weight、conservative_reuse、fused_score](../../../service/runtime_monitor/core/resident_reuse.py) 给出评分的基础函数。
- [TorchScript 权重](../v4.1/model/rankprop_v4_1.ts) 与当前服务加载文件的 SHA-256 一致。

## 评分逻辑

时序分数 e 通过 `1 / (1 + exp(4 * (e - 0.5)))` 转成保护权重。复用估计分别维护 file 和 anon，按当前驻留量加权，并结合有效采样覆盖率和置信度保留未知部分的保护。最终分数取时序权重与有效复用率乘积、近期活动下限中的较大值。

进入前台后的首个四秒窗口使用历史旧页复用信息。其后，观测充分的前台应用根据近期实际活动调整保护。完成且覆盖有效的窗口通过在线均值更新复用估计。服务继续提供独立的下一应用概率字段供需求预测使用。

`fuse` 接收常驻服务提供的应用绑定、已学习的复用统计和当前活动观测。Linux 事件采集、页观测与执行控制由常驻服务提供。这里发布当前预测 API、融合实现及其基础模块，便于直接查看和复用评分函数。

基础函数可从仓库根目录调用

```bash
PYTHONPATH=lzx/service/runtime_monitor python3 -c 'from core.resident_reuse import temporal_weight, conservative_reuse, fused_score; print(fused_score(temporal_weight(0.5), conservative_reuse(0.2, 0.95), 0.1))'
```

`source_manifest.json` 记录本次发布各实现文件与运行目录源文件的摘要。训练脚本仅调整仓库根路径解析以支持其他 checkout 位置。
