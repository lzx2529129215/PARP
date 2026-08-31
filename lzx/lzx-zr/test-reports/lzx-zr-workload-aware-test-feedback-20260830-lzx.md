# lzx-zr Workload-Aware 原型 — 测试反馈文档

> 日期:2026-08-30
> 执行环境:Ubuntu VM `zr@zr-virtual-machine`(内核 6.8.0-134,Python 3.10.12)
> 代码路径:`lzx/lzx-zr/`(纯标准库实现,零第三方依赖)
> 结论:**全部通过。** unittest 5/5 通过;CLI 主流程在 SHADOW/OBSERVE × rule_trend/second_order_markov 三种组合下均成功产出 4 类输出文件。

---

## 1. 测试执行摘要

| 项目 | 结果 |
|---|---|
| 代码同步 | `scp -r lzx/lzx-zr ubuntu-vm:~/lzx-zr` 成功(16 个 `.py` 全部到位) |
| 依赖检查 | 仅 `argparse/json/math/sys/time/unittest/collections/dataclasses/pathlib/typing`,无外部依赖 |
| unittest | **5/5 通过**,耗时 0.004s |
| CLI SHADOW + rule_trend | 成功,产出 4 文件 |
| CLI SHADOW + second_order_markov | 成功,产出 4 文件 |
| CLI OBSERVE + rule_trend | 成功,产出 4 文件 |
| TTL 新鲜度守卫 | 已验证:过期快照 → `NATIVE`,新鲜快照 → `SHADOW` |

---

## 2. 代码结构与数据流

### 2.1 模块职责总览

| 文件 | 职责 |
|---|---|
| `tools/run_workload_aware.py` | CLI 入口 + 流水线编排 |
| `runtime_monitor/features/engine.py` | 原始观测 → 特征向量(访问/复用/热点/工作集/压力 5 类) |
| `runtime_monitor/detector/state_machine.py` | 特征 → 状态分类 + 驻留(dwell)防抖 |
| `predictor/workload_predictor.py` | 当前状态 → 下一状态预测(规则趋势 / 二阶马尔可夫) |
| `runtime_monitor/output/snapshot.py` | 预测 → 结构化快照 + 落盘 |
| `kernel/adapters/parp_snapshot_adapter.py` | 快照 → PARP 影子提示(带 TTL 新鲜度守卫) |
| `tests/unit/test_workload_aware.py` | 5 个单元测试 |
| `configs/workload_aware.yaml` | 阈值/参数配置 |
| `kernel/uapi/workload_prediction_v1.json` | 快照 UAPI schema |

### 2.2 数据流(端到端)

```
observations.jsonl
   │  load_observations()  逐行 JSON → Observation 对象
   ▼
Observation ── extract_features() ──► FeatureVector(5 类特征)
   ▼
StateMachine.update() ── classify() ──► WorkloadState(4 维状态 + 置信度)
   ▼
WorkloadPredictor.predict_rule_trend() / predict_markov() ──► Prediction
   ▼
make_snapshot() ──► snapshot dict
   │
   ├─► features.jsonl     (特征 + 状态,逐窗口)
   ├─► predictions.jsonl  (快照,逐窗口)
   ├─► prediction_snapshot.json  (最后一条原始快照,无守卫)
   └─► parp_shadow_hint.json     (经 TTL 守卫过滤后的影子提示)
```

---

## 3. 逐段代码作用与效果

### 3.1 `tools/run_workload_aware.py`(入口 / 编排)

**`ROOT = Path(__file__).resolve().parents[1]` + `sys.path.insert`**
- 作用:把 `lzx-zr` 目录加入模块搜索路径,使脚本能从任意 cwd 以 `from runtime_monitor...` 引用本地包。
- 效果:脚本不依赖安装,VM 上 `python3 tools/run_workload_aware.py` 直接可跑。

**`load_observations(path)`**(第 19–37 行)
- 作用:逐行读 JSONL,把每个 JSON 对象映射成 `Observation` 数据类。每个字段用 `row.get(...)` 取默认值做容错:`scope_type` 缺省 `cgroup`、`region_ids` 缺省 `()`、`counters` 缺省 `{}`。
- 效果:fixture 里 3 行观测被完整解析(第 3 行故意缺 `region_ids`/`region_accesses`,仍能解析成功并进入后续流程)。

**`main()` 里的 `argparse`**(第 41–46 行)
- 作用:声明 4 个参数——`--input`、`--output-dir`(必填)、`--mode`(`OBSERVE`/`SHADOW`,默认 OBSERVE)、`--method`(`rule_trend`/`second_order_markov`,默认 rule_trend)。
- 效果:同一份代码通过参数即可切换「观察 vs 影子」和「规则 vs 马尔可夫」两条预测路径。

**流水线主循环**(第 47–63 行)
- 作用:每个观测窗口依次执行 `extract_features → machine.update → observe_transition → predict → make_snapshot`,并把「特征+状态」写 `features.jsonl`、「快照」写 `predictions.jsonl`。
- 效果:3 个窗口 → 3 行 features + 3 行 predictions。

**收尾落盘**(第 64–68 行)
- 作用:取 `predictions.jsonl` 最后一行,分别写 `prediction_snapshot.json`(原始快照)和 `parp_shadow_hint.json`(经守卫)。
- 效果:前者是「最后窗口的预测快照」,后者是「经 TTL 校验后的可用提示」——两者在本 demo 中因时间戳过期而产生差异(见 5.1)。

---

### 3.2 `runtime_monitor/features/engine.py`(特征提取)

**常量与 `q15()`**(第 8–18 行)
- 作用:`Q15_ONE = 32767`;`q15(v)` 把 `[0,1]` 浮点概率量化到 `[0,32767]` 的 Q15 定点整数。
- 效果:概率/置信度以整数形式进入快照,兼容内核侧 Q15 约定。`q15(-1)=0`、`q15(2)=32767`(越界截断,已由测试覆盖)。

**`_num()` / `_safe_ratio()`**(第 21–30 行)
- 作用:`_num` 安全取数值(非有限值回退默认);`_safe_ratio` 分母 ≤0 时返回 0,避免除零。
- 效果:保证任意脏/缺字段都不会让特征提取崩溃。

**`Observation` / `FeatureVector`**(第 33–66 行)
- 作用:两个 `frozen` 数据类。`Observation` 承载输入信号(scope、窗口、region 序列、访问次数、时间戳、counters);`FeatureVector` 承载 5 组特征 + 数据质量。
- 效果:类型清晰,不可变,便于状态机无副作用地消费。

**`extract_features()`**(第 68–149 行)——核心,分 5 组:
- **access(访问模式)**:`adjacent_region_ratio`(相邻同区比例)、`direction_consistency`(地址单调向前/向后一致性)、`sequential_run_length`(最长连续不同区游程)、`spatial_locality`、`address_entropy`(访问分布香农熵)。
- **reuse(复用模式)**:`reuse_distance_peak`(复用距离众数)、`reuse_distance_stability`、`access_periodicity`(时间间隔稳定性)、`reuse_rate`、`single_access_page_ratio`(只访问一次的页占比)。
- **hotspot(热点)**:`hotspot_count`、`hotspot_concentration`(最大访问占比)、`hotspot_jaccard`/`hotspot_shift_rate`(来自 counters)。
- **working_set(工作集)**:`wss_pages`、`wss_slope_pages_per_sec`(增长斜率)、`region_count`、`anon_file_ratio`。
- **pressure(压力)**:`allocation_rate`、`page_fault_rate`、`refault_rate`、`psi`、`direct_reclaim`、`pgscan`/`pgsteal`、`pswpin`/`pswpout`、`foreground`。
- **data_quality**:`has_region_order`、`region_resolution`(HIGH/LOW)、`observation_count`。

效果(实测第 1 窗口):`region_ids=[r1,r2,r3,r4]`、访问各 1 次 → `address_entropy=2.0`(4 个均匀桶)、`direction_consistency=1.0`、`single_access_page_ratio=1.0`、`wss_pages=400`。特征成功把「4 个均匀分布、单向推进」的访问序列量化了出来。

**辅助函数** `_reuse_pairs/_mode/_stability/_periodicity/_longest_run`(第 152–188 行):分别为复用距离配对、取众数、变异系数稳定性、时间间隔稳定性、最长游程——是上面各特征的底层算子。

---

### 3.3 `runtime_monitor/detector/state_machine.py`(状态分类 + 防抖)

**`WorkloadState`**(第 9–19 行)
- 作用:状态数据结构,含 4 维(`access_order`/`reuse_mode`/`hotspot_mode`/`phase_mode`)+ `dominant`(主导态)+ `confidence_q15` + `reason` + `state_changed`。

**`StateMachine.update()`**(第 31–54 行)——驻留/冷却防抖,是本文件最关键的逻辑:
- 作用:用 `min_dwell_windows`(默认 2)、`cooldown_windows`(默认 1)、`confidence_threshold`(0.55)三个参数实现迟滞:新状态必须连续出现 ≥2 个窗口才切换(`_candidate_windows >= min_dwell_windows`),切换后进入 1 窗口冷却(`_cooldown`),避免单一异常窗口导致状态抖动(flapping)。
- 效果(实测):fixture 第 3 行**没有** region 顺序(`has_region_order=False`),按 `classify` 应归为 `UNKNOWN`,但状态机因为第 3 行只是「候选」尚未满 2 个窗口,仍然**维持**上一个 `BURST_EXPANSION` 状态,且 `state_changed=false`。这正是防抖机制起作用的直接证据。

**`classify()`**(第 57–103 行)——打分判定:
- 作用:先算 `sequential_score` / `random_score`,二者过阈值(0.55)定 `access_order`;再依次定 `reuse_mode`(CYCLIC/HIGH_REUSE/ONE_SHOT/UNKNOWN)、`hotspot_mode`(SHIFTING/MULTI/SINGLE/UNKNOWN)、`phase_mode`(EMERGENCY/EXPANDING/STREAMING/COLD/STABLE);最后按优先级链决定 `dominant`,并用「4 维中有几维非 UNKNOWN」计算置信度。
- 效果(实测):第 1、2 窗口被判定为 `dominant=BURST_EXPANSION`(因为 `wss_slope>0` 触发了 EXPANDING → BURST_EXPANSION),`confidence_q15=32767`(4 维全部已知)。

---

### 3.4 `predictor/workload_predictor.py`(预测)

**`Prediction`**(第 11–20 行)
- 作用:预测结果结构,含 `current`/`next` 两个状态 + `probability_q15` + `horizon_ms`/`ttl_ms` + `prediction_seq` + `model_version` + `method`。

**`WorkloadPredictor.__init__`**(第 24–29 行)
- 作用:默认 `horizon_ms=3000`、`ttl_ms=5000`、`model_version=1`,并初始化 `_seq=0`、`_transitions`(二阶转移计数表)。

**`observe_transition()`**(第 31–32 行)
- 作用:记录 `(prev.dominant → current.dominant)` 转移,累计到 `defaultdict(Counter)`,供马尔可夫模型使用。

**`predict_rule_trend()`**(第 34–41 行)
- 作用:规则启发式——当前若是 `EXPANDING` 或 `EMERGENCY`,就预测下一阶段回落到 `STABLE`/`STABLE_HOT`(扩张后趋于稳定、紧急后趋于稳定);否则预测保持当前状态。
- 效果(实测):`next_workload.dominant = STABLE_HOT`(从 BURST_EXPANSION 扩张态预测进入稳定热点)。

**`predict_markov()`**(第 43–51 行)
- 作用:二阶马尔可夫——取 `previous.dominant` 与 `current.dominant` 构成的转移历史,查最可能的下一个 `dominant`;无历史时回退 0.5 概率并保持当前态。
- 效果(实测):`prediction_seq=1` 时无历史 → `probability_q15=16384`(即 0.5);`seq=2、3` 时历史为「BURST_EXPANSION → BURST_EXPANSION」→ 预测下一态仍是 `BURST_EXPANSION`,`probability_q15=32767`(1.0)。与 rule_trend 的「回落 STABLE_HOT」形成鲜明对比,证明两条预测路径确实不同。

---

### 3.5 `runtime_monitor/output/snapshot.py`(快照)

**`make_snapshot()`**(第 12–46 行)
- 作用:把 `Prediction` 拍平成带 `current_workload`/`next_workload` 的字典,附 `wss_pages`、`horizon_ms`、`ttl_ms`、`prediction_seq`、`model_version`、`method`、`mode`、`native_fallback`、`generated_at_ns`(真实墙钟纳秒)。
- 效果:快照是自描述的,下游(适配器)无需访问对象图,直接读字典即可。

**`write_snapshot()`**(第 49–52 行)
- 作用:`mkdir -p` + 缩进 2 的 JSON 落盘。
- 效果:产出人类可读的 `prediction_snapshot.json`。

---

### 3.6 `kernel/adapters/parp_snapshot_adapter.py`(影子提示 + TTL 守卫)

**`snapshot_is_usable()`**(第 11–30 行)——关键守卫:
- 作用:5 道校验,只接受「新鲜、有类型、非 UNKNOWN」的预测——① mode 必须是 OBSERVE/SHADOW;② `apply` 或 `native_fallback` 为真则拒绝;③ `ttl_ms`/`timestamp_ns` 必须有效且**未过期**(`now > timestamp + ttl` 则拒绝);④ 当前/下一状态的 dominant 不能是 UNKNOWN/MIXED;⑤ confidence/probability 必须在 Q15 范围内。
- 效果(实测,见 5.1):demo fixture 的 `timestamp_ns=4000000000`(≈1970 年)远早于真实墙钟,`now > timestamp + ttl*1e6` 成立 → 判定过期 → 回退 `NATIVE`。

**`to_parp_shadow_hint()`**(第 33–51 行)
- 作用:把快照转成 PARP 影子提示。有效时 `protection_hint=protect_next_workload`、`reclaim_hint=preserve_hot_regions`、`mode=SHADOW`;无效时全部回退 `native`、`mode=NATIVE`。`apply` 恒为 `False`(只观察,不落地内核)。
- 效果:提示是「有界、只观察」的,严格遵守「不执行 APPLY」的边界(见 README 边界)。

**`write_hint()`**(第 54–57 行)
- 作用:落盘 `parp_shadow_hint.json`。

---

### 3.7 `tests/unit/test_workload_aware.py`(5 个用例)

| 用例 | 验证点 | 结果 |
|---|---|---|
| `test_q15_is_bounded` | Q15 量化越界截断(-1→0,2→32767) | ok |
| `test_missing_region_order_is_unknown` | 无 region 顺序 → access_order/dominant=UNKNOWN、置信度=0 | ok |
| `test_state_machine_requires_dwell` | 状态切换需驻留 2 窗口(首窗不 flip) | ok |
| `test_snapshot_is_shadow_only` | SHADOW 模式 + native_fallback 标志 + prediction_seq=1 | ok |
| `test_stale_or_unknown_snapshot_falls_back_to_native` | 过期快照 → `snapshot_is_usable=False` → hint 回退 NATIVE | ok |

### 3.8 配置与 schema

- `configs/workload_aware.yaml`:集中声明 `horizon_ms=3000`、`ttl_ms=5000`、`min_dwell_windows=2`、`cooldown_windows=1`、`confidence_threshold=0.55`、`emergency_psi=0.20` 等阈值。**注意**:当前代码里 `StateMachine` 用的是类默认值(2/1/0.55),并未实际从该 YAML 加载——YAML 目前是「参数说明文档」性质,而非运行时配置源(见 6.2 建议)。
- `kernel/uapi/workload_prediction_v1.json`:定义快照必需字段(`scope_type/scope_id/timestamp_ns/current_workload/next_workload/horizon_ms/ttl_ms/prediction_seq/model_version`)、`modes=[OBSERVE,SHADOW]`、`q15_one=32767`、`apply=false`。

---

## 4. 测试结果明细

### 4.1 unittest 输出

```
test_missing_region_order_is_unknown ... ok
test_q15_is_bounded ................... ok
test_snapshot_is_shadow_only .......... ok
test_stale_or_unknown_snapshot_falls_back_to_native ... ok
test_state_machine_requires_dwell ..... ok

Ran 5 tests in 0.004s
OK
```

### 4.2 CLI 三组合产出

每次运行都在 `outputs/<dir>/` 下生成 4 个文件:`features.jsonl`、`predictions.jsonl`、`prediction_snapshot.json`、`parp_shadow_hint.json`。

**rule_trend vs second_order_markov 的 `next_workload` 差异**(同一份 fixture):

| 方法 | seq=1 的 next.dominant | seq=1 的 probability_q15 | 语义 |
|---|---|---|---|
| rule_trend | `STABLE_HOT` | 32767(1.0) | 扩张态 → 预测回落稳定热点 |
| second_order_markov | `BURST_EXPANSION` | 16384(0.5) | 无历史 → 保守保持,概率 0.5 |

**OBSERVE vs SHADOW**:`mode` 字段相应变为 `OBSERVE`/`SHADOW`,其余字段一致(快照结构相同)。

---

## 5. 关键发现

### 5.1 ⭐ TTL 新鲜度守卫实际生效(demo 提示恒回退 NATIVE)

`parp_shadow_hint.json` 的 `mode` 是 **`NATIVE`**,而 `prediction_snapshot.json` 的 `mode` 是 `SHADOW`。这不是 bug,而是 `snapshot_is_usable()` 的 TTL 守卫在工作:

- fixture 的时间戳是**合成纳秒值** `1000000000~4000000000`(对应 1970 年),而守卫用真实 `time.time_ns()`(2026 年,约 1.79e18)比对。
- `now(1.79e18) > timestamp(4e9) + ttl(5e9)` → 判定过期 → 回退 `NATIVE`。

**验证实验**(把时间戳改成「1 秒前」):

| 快照时间戳 | `snapshot_is_usable` | `hint.mode` | `protection_hint` |
|---|---|---|---|
| 4000000000(过期) | False | NATIVE | native |
| now - 1s(新鲜) | True | **SHADOW** | protect_next_workload |

结论:守卫逻辑正确。**若要让 demo 真实产出 `SHADOW` 提示,fixture 的 `timestamp_ns` 需改为接近 `time.time_ns()` 的真实值**(当前合成值故意演示了「过期快照被安全拒绝」的路径)。

### 5.2 状态机驻留(dwell)防抖生效

fixture 第 3 行无 region 顺序,按 `classify` 应归 `UNKNOWN`,但状态机因候选未满 2 窗口而维持 `BURST_EXPANSION`,`state_changed=false`。这证明「单窗口异常不触发状态翻转」的设计目标达成。

### 5.3 两条预测路径行为差异可复现

rule_trend 给出「扩张→稳定」的趋势外推;二阶马尔可夫在冷启动时概率 0.5、有历史后概率 1.0 地保持当前态。两者对同一输入的 `next_workload.dominant` 不同,证明 `--method` 参数确实切换了预测逻辑。

---

## 6. 结论与建议

### 6.1 结论

- 原型在 VM(Python 3.10)上**零依赖、可运行、全部测试通过**。
- 特征提取、状态分类(含防抖)、预测、快照、影子提示(TTL 守卫)五个环节串联正确,端到端数据流无断点。
- 边界约束(只 OBSERVE/SHADOW、`apply=false`、不写内核)在代码和输出中均得到遵守。

### 6.2 建议(非阻塞)

1. **fixture 时间戳**:若要演示真实 `SHADOW` 提示,把 `tests/fixtures/observations.jsonl` 的 `timestamp_ns` 换成 `time.time_ns()` 附近的值(当前合成值触发 TTL 回退,恰好验证了守卫,但演示不了 SHADOW 正向路径)。
2. **配置未接入**:`configs/workload_aware.yaml` 与代码里 `StateMachine` 的硬编码默认值(2/1/0.55)是脱节的,建议让 CLI 从 YAML 读取 `horizon_ms/ttl_ms/min_dwell_windows/cooldown_windows/confidence_threshold`,避免两处漂移。
3. **cooldown 参数在当前 fixture 未被触发**:因为 3 个窗口不足以让状态翻转两次,建议增加一个「明确状态跳变」的 fixture 覆盖冷却路径,提升 `StateMachine` 的测试充分度。

---

*本文档由 Claude Code 于 2026-08-30 在 VM 上实测生成。*
