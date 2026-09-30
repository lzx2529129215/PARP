# Release audit

## Release 文件清单

| 路径 | 用途 | 是否修改 |
|---|---|---|
| `README.md` | 新用户使用手册 | 新增 |
| `RELEASE_AUDIT.md` | 本发布审计 | 新增 |
| `LICENSE_PLACEHOLDER.md` | 未选定 License 的明确占位 | 新增 |
| `.gitignore` | 排除构建、run、日志、secret | 新增 |
| `requirements.txt` | 最小 Python 依赖说明 | 新增 |
| `Makefile` | 根目录构建入口 | 多架构 include 推导修改 |
| `ebpf/app_fault.h` | BPF/loader 共享定义 | 原样复制 |
| `ebpf/app_fault.bpf.c` | PageFault BPF 程序 | 原样复制 |
| `ebpf/app_fault_user.c` | libbpf loader | 原样复制 |
| `scripts/phase8_collector.py` | legacy WSS collector | 原样复制 |
| `scripts/phase8_collector_real_wss_split.py` | strict real-WSS collector | 原样复制 |
| `scripts/phase7_workload.py` | generic workload | 默认输出目录可移植性修改 |
| `scripts/setup_app_cgroup.sh` | 创建 target cgroup | 原样复制 |
| `scripts/launch_app_in_cgroup.sh` | 应用出生即入 cgroup | bash 调用/日志目录可移植性修改 |
| `scripts/check_environment.py` | 通用环境检查 | 新增 |
| `scripts/quick_start.sh` | 非 WPS 最小端到端验证 | 新增 |
| `automation/__init__.py` | Python package 标记 | 原样复制 |
| `automation/wps_90_plan.py` | 90-event 示例定义 | 原样复制 |
| `automation/wps_actions.py` | WPS GUI 基元 | GUI/session 与保存路径可移植性修改 |
| `automation/wps_representative_automation.py` | WPS 18-operation 示例 | 项目、样本、helper 路径可移植性修改 |
| `automation/wps_runner.py` | 90-event runner | 样本与保存路径可移植性修改 |
| `automation/image_clipboard.py` | 可选 WPS 图片粘贴 helper | 从实际依赖项目复制 |
| `dataset/representative_operations.csv` | 18-operation 定义 | 原样复制 |
| `config/example.env` | 私有环境变量模板 | 新增 |
| `config/paths.example.yaml` | 路径配置说明 | 新增 |
| `docs/architecture.md` | 三条采集链路 | 新增 |
| `docs/metrics.md` | 字段/单位/限制 | 新增 |
| `docs/environment.md` | 环境与权限 | 新增 |
| `docs/validation.md` | 18-operation 验证边界 | 新增 |
| `examples/example_summary.json` | 已验证 summary 小样例 | 原始真实样例，无个人绝对路径 |
| `examples/example_18ops_result.csv` | 已验证 18 行结果样例 | 原始真实样例，无个人绝对路径 |
| `samples/README.md` | 大样本文件准备说明 | 新增 |

## 硬编码修改

| 原方案 | Release 方案 |
|---|---|
| 固定项目、样本目录 | `Path(__file__)` 相对根目录 + `WPS_SAMPLES_DIR` / `PHASE9_*` |
| 固定 GUI UID/display/session | 仅读取调用者环境，缺失时提示 |
| 外部图像 helper 路径 | release 内 `automation/image_clipboard.py` |
| 固定临时保存目录 | 调用者路径或相对 `runs/` |
| 固定 x86 system include | `gcc -print-multiarch` 与 `uname -m` 推导 |

## 硬编码审计计数

- 初始审计发现的可移植性硬编码/固定布局组：15；
- release 副本已修复：15；
- 发布前复扫结果：0 个用户目录、固定 UID/GUI session、Windows 盘符、固定 WPS 二进制或外部项目路径残留。

空的 GUI 环境变量键位于 `config/example.env`，用于提示使用者自行设置，不包含机器具体值；README 中的项目名不是路径。

## 验证

验证在独立 staging 副本完成，原项目未修改。

| 检查 | 结果 | 证据 |
|---|---|---|
| Python compile | PASS | `python3 -m py_compile scripts/*.py automation/*.py` |
| Python imports / automation static check | PASS | 18-operation `--dry-run`、90-event `--validate-plan` |
| Shell syntax | PASS | `bash -n scripts/*.sh` |
| Build | PASS | `make clean && make` 生成 BPF object 与 loader |
| Collector CLI | PASS | loader 与 real-WSS collector 的 `--help` |
| Environment checker | PASS | mandatory prerequisites ready；BTF、llvm-config 仅 WARN |
| Quick start | PASS | 新 cgroup + 16 MiB anon workload + 5 秒 collector window |
| Quick start summary | PASS | `real_wss_total_bytes`、`rss_total_bytes`、`page_fault_total_count` 均存在，`data_quality_status=PASS` |

quick start 的测试输出仅存在于 staging 临时目录，未进入 release 文件夹或 ZIP。

## 未解决限制

- 非 CO-RE；kprobe hook 依赖目标内核符号；
- WPS GUI automation 为 optional；
- real WSS 是 clear_refs + smaps Referenced proxy，不是直接 PTE ground truth；
- Anon/File 是 VMA/residency-assisted 分类；
- PageFault Anon/File 是 handler-path observation。
