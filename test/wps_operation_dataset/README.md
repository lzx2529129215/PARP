# wps_operation_dataset — Phase 0 / Phase 1

Phase 0 / 1 提取 GUI-360 Office 轨迹中的文本 action 信息。新增的离线语义分析仅使用现有 121 条轨迹、753 条记录，输出 WPS Operation Space v1 候选版；不进行 GUI 回放、转移概率学习、LSTM 或 WSS 训练。

## 当前离线分析（不再下载）

授权扩展后的首批实现位于 `automation/wps_replay.py`，通过现有 `app_automation.py` 的
`wps_replay_operation` 动作调用。`outputs/executable_wps_batch_v2/` 包含 NOW 重算、C/D 列独立升序
三条 AgentNet 任务的替代工作簿、场景、结果断言和能力定义。它们已具备试运行条件，GUI 状态仍为 `GUI_NOT_RUN`；
`executable_wps_space_v1` 是扩展前的历史快照，不代表新增能力后的状态。本轮没有启动 WPS。

现有自动化能力对齐入口：
`PYTHONPATH=.deps PYTHONDONTWRITEBYTECODE=1 python3 -m wps_operation_dataset.executable_alignment`。
静态解析实际函数、注册表、素材和历史 UI 审计，在 `outputs/executable_wps_space_v1/` 输出能力与 AgentNet 逐步骤绑定。
不导入 GUI runner，不启动 WPS，不继续 65/30 分类。固定复合场景、原语、占位声明和完整任务可执行性分别判断。

AgentNet 语义序列分析新增入口：
`PYTHONPATH=.deps PYTHONDONTWRITEBYTECODE=1 python3 -m wps_operation_dataset.agentnet_sequences`。
只使用 `/home/lzx/Desktop/AgentNet/` 的已有文本候选及初筛结果；输出 `outputs/agentnet_sequences_v1/`。
自动候选和完整任务人工文本审阅子集分别报告。保留 OOV/未知断点，按任务计算一阶、二阶及长度 4/5 子序列条件频率，
不拼接任务，不把 click 序列视为语义事件序列，不训练模型或重放。当前概率不能视为一般用户行为概率。

新增两层操作空间：保留原 65 个细标签，映射到 30 个按处理机制假设设计的 WSS token。
运行 `PYTHONPATH=.deps PYTHONDONTWRITEBYTECODE=1 python3 -m wps_operation_dataset.wss_tokens_v1`，
产物位于 `outputs/wss_operation_space_v1/`。保留细标签、证据层级与来源；不生成虚构会话或 WSS 标签。
详见该目录 `REPORT.md` 与 `operation_space.json`。未使用实测 WSS 聚类，不代表已验证归并后的预测效果。

```bash
PYTHONPATH=.deps PYTHONDONTWRITEBYTECODE=1 python3 -m wps_operation_dataset.analyze_offline
```

入口不调用 remote/cli/extract，也不跟随文本中的资源链接。输入绑定现有 Parquet 的 SHA-256；变化时拒绝沿用人工标注。全部写入经过 DiskGuard，源数据保持不变。

结果见 `outputs/analysis_v1/REPORT.md`，包括原始动作分布、主任务意图分布、有动作记录的语义覆盖、候选边界和逐轨迹问题。`semantic_v1.py` 保存版本化人工标注；CSV、派生 Parquet 与 JSON 保留精确来源关联。候选参数为槽位定义，原值保留在源 action args 和 request 中。

每个候选计数的是支持它的轨迹，不是独立完成操作次数。结束标记不等于动作，实际动作也不等于完成证明。当前样本不是自然用户频率，WPS 兼容性均未验证。

`manifest.json` 最后发布，记录产物校验和；重新分析前撤销旧 manifest，防止中断时把混合产物当作完整版本。若中断留下 `.tmp/offline-*`，需检查对应临时文件后移走，再重跑；不会覆盖未检查的临时文件。以下下载命令仅保留为前两阶段历史用法，当前工作无需运行。

## 边界

固定来源：`vyokky/GUI-360`。先通过 `/refs` 获取 main 的提交 ID，之后所有 inventory 与 JSONL 均绑定该提交。目录 API 只查询以下三个目录（非递归，支持分页），数据入口仅接受其直接子级 `.jsonl`：

```text
train/data/word/in_app/success/*.jsonl
train/data/excel/in_app/success/*.jsonl
train/data/ppt/in_app/success/*.jsonl
```

不遍历仓库根目录，不访问 image、fail、processed_data、search、online、test 或 template 数据路径。不使用 datasets 自动配置、snapshot_download、git clone、huggingface-cli 或任何全量下载 API。

**JSONL 内可能嵌有截图/可访问性数据。HTTP 读取该 JSONL 时无法避免这些字段的传输字节；本实现用事件解析器丢弃它们，不生成完整原始记录、不保存它们、不跟随资源链接，也不请求独立截图/音视频/a11y 文件。** 若要求这些字段连网络传输都不能出现，需要源端提供字段投影或纯 action 文件；通用 HTTP 文件流无法实现源端列裁剪。

## 磁盘保护

`DiskGuard` 的默认硬限制如下，CLI 不提供放宽选项：

```ini
min_free_gb = 20
max_workspace_gb = 5
max_temp_mb = 256
```

GB/MB 参数按 GiB/MiB 解释。每次数据写入前重新检查剩余空间、项目总文件大小、临时目录大小和本次写入预计增量。CSV、JSON、Parquet 头/数据块/页脚都经过 GuardedWriter。临时文件统一位于 `.tmp/`；拒绝符号链接和目录逃逸。输出不覆盖已有数据。Parquet 完成并关闭后原子发布；失败时仅保留受配额保护的 `.part`，不会冒充完整数据。

保护针对本进程写入；无法阻止其他进程在检查后并发占满磁盘。原始远程文件不会落盘，不启用文件缓存。单文件传输上限 256 MiB，解析使用有界读取；action args 和文本另有大小限制，超限中止而非默默截断。

## 环境与运行

开发环境依赖已装在本目录 `.deps/`，包含 pytest、ijson、pyarrow；requests 使用系统已有版本。使用 `PYTHONDONTWRITEBYTECODE=1` 禁止隐式 pycache 写入。

```bash
cd /home/lzx/Desktop/PARP/test/wps_operation_dataset
export PYTHONPATH="$PWD/.deps"
export PYTHONDONTWRITEBYTECODE=1

# Phase 0：只请求远程元数据，输出完整路径清单、大小与固定版本。
python3 -m wps_operation_dataset.cli plan

# 查看 outputs/inventory/access_plan.json 与 gui360_files.csv。
# 用户检查路径计划后，Phase 1 使用该计划的 SHA-256：
python3 -m wps_operation_dataset.cli extract --reviewed-plan-sha256 <计划SHA256>
```

`plan` 不读取远程 JSONL，已存在的 inventory 不覆盖；需要更新时先保留旧输出或使用新 workspace。SHA-256 是防止计划意外变更的技术校验，不代替用户检查。未获得本次用户的路径检查结果前，不执行 extract。

## 计数与裁剪

GUI-360 的 JSONL 一行通常是一个 step，而非一个完整 trajectory。按 `(app_domain, execution_id)` 去重计数，每个应用最多 2000；step 数不受 2000 行限制。当前文件中已选中 ID 的剩余 step 会继续保留。达到上限后不再打开该应用后续文件。默认每应用最多规划字典序前 2000 个文件，这是有界提取策略，不保证随机代表性，也不保证每应用一定能凑满 2000。

发布格式假定一个 execution 的 steps 位于该轨迹文件内；若同一 execution 分散在未读取的文件，无法保证补全。跨文件重复 execution ID 不增加 trajectory 数，原始 step 的重复记录不自动删除。

事件解析期间仅构建必要字段，行裁剪后用至多 128 行或约 1 MiB 文本批次写 Parquet。`args` 保存为 JSON 文本，过滤嵌套截图/音视频/a11y 字段及内嵌媒体 data URI。只保留：

```text
execution_id, app_domain, request, step_id, action_type,
control_text, control_label, function, args, status
```

`step_id` 为整数；其余列为可空字符串。`status` 来源于 `step.status`，不以 success 路径伪造每一步成功。

## 输出

- `outputs/inventory/gui360_files.csv`：允许目录的文件 inventory，含 planned 标记。
- `outputs/inventory/access_plan.json`：完整计划、提交、计数和预计大小。
- `data/curated/gui360_office.parquet`：真实提取后才生成；Phase 0 不生成空文件冒充完成。
- `outputs/storage_report.json`：阶段状态、去重轨迹数、请求审计、媒体计数、磁盘状态及配额验证。

待用户检查时，report 的 `extraction_completed=false`，真实提取计数为 0。本地 pytest 使用合成 JSONL，测试产物不会混入真实 curated 输出。

## 测试

```bash
PYTHONPATH=.deps PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --basetemp=.tmp/pytest-tests
```

覆盖磁盘动态变化、写入预计增量、临时配额、路径逃逸、分页逃逸、流式短读取、字段投影与嵌套敏感字段裁剪、应用不匹配、6000 条合成轨迹的三应用上限、step/trajectory 区分、达到配额后停止打开文件、损坏数据不发布、计划修改、远程内容读取隔离和 Parquet schema。测试目录是临时测试专用，测试后清理以更新真实存储报告。

## 本次真实提取验收

已按用户检查的 121 个文件完成提取：Word 63 条/376 步，Excel 21 条/128 步，PPT 37 条/249 步。共 121 条 trajectory、753 条 action step。

固定版本的原始字段为 `step.action.control_test`，已兼容映射为输出 `control_text`，输出不新增字段。首次结果发现此差异后，在同一批准路径范围内重读修复；一次代理错误导致的失败修复没有覆盖正式文件。额外读取与最终 SHA-256 见存储和验收报告。

47 项 pytest 通过；真实输出逐行验证列白名单、轨迹上限、step 唯一性与顺序、结束状态、args 裁剪、路径范围和磁盘限制。详见 `outputs/extraction_validation.json`。
