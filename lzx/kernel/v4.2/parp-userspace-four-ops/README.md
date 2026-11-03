# PARP 用户态四操作提前回收

使用迁移前的用户态控制器，读取后续四个操作的 WSS、缺页次数，再控制后台应用的内存回收。控制器与查表核心源码按原文件逐字节复制，回收逻辑未改。

## 代码入口

| 文件 | 用途 |
|---|---|
| `operation_controller.py` | 分类、预算、保护；写目标后台 cgroup 的 `memory.high` / `memory.reclaim` |
| `operation_lookup.py` | 调用 C 动态库，或读取等价 JSON 查表 |
| `get_wss_predictor.c/.h` | 原始四操作查表接口 |
| `gimp_lookup_data.h`、`data/operation_table.json` | 随附 GIMP 查表数据 |
| `docs/LOGIC.md` | 当前策略的实际行为 |
| `run_operation_tests.py`、`test_*.py` | 原有接口与控制逻辑测试 |
| `SOURCE_MANIFEST.json` | 源码哈希及来源记录 |

## 环境

- Python 3.10 或更新版本；运行控制器只使用 Python 标准库。
- 执行实际回收需要 Linux cgroup v2、`memory.reclaim` 以及目标组文件的写权限。
- 后台应用应已放入目标 cgroup；本控制器不启动应用、不自动识别操作，也不迁移进程。
- 编译 C 查表库和运行完整测试需要 C 编译器。无需编译 PARP 自定义内核。

## 1. 先查表，不执行回收

在项目根目录运行：

```sh
python3 operation_controller.py --query gimp 5 1 8 --rss-bytes 1073741824 --reuse-fault-max 10000 --turnover-burst-min 50000
```

这里的 1 GiB RSS 仅用于演示查询。实际接入须传入与预测范围一致的当前后台 RSS。

## 2. 使用原 C 查表函数

```sh
make
python3 operation_controller.py --library ./build/libwss_predictor.so --wss-unit bytes --query gimp 5 1 8 --rss-bytes 1073741824 --reuse-fault-max 10000 --turnover-burst-min 50000
```

本包原 C 表返回 **bytes**，成功码为 **0**。不要再次乘 MiB；其他外部库须按其真实单位显式设置 `--wss-unit`。

每个数组位置对应一个未来操作的总量；不是四个操作相加后的值。`current_op_id=k` 返回 k+1 开始的最多四个阶段，不足四个时尾部成对补 `-1`。压力档为 `0/8/12/14` GiB，与当前空闲内存不是同一个量。

## 3. 实际控制后台 cgroup

目标路径须换成实际后台组；两项缺页阈值是此前实验参数，并非所有应用的通用标定值：

```sh
python3 operation_controller.py --library ./build/libwss_predictor.so --wss-unit bytes --apply --cgroup /sys/fs/cgroup/your-test/background --reuse-fault-max 10000 --turnover-burst-min 50000 --lease-seconds 180
```

运行后由工作负载驱动向标准输入逐行发送 JSON，并保持输入管道打开：

1. 发 `forecast`，包含应用、序列、当前操作号、压力档及实际 RSS；读取输出中的 `generation`。
2. 第一个预测操作真正开始时发 `enter step=0`，随后真实阶段依次发 `step=1/2/3`。
3. 四个阶段结束时发 `step=4`；若只有 N 个有效结果，则发 `step=N` 结束。
4. 顺序改变或提前退出时发 `stop`。同一会话重复查同一锚点不能补充预算。

事件格式见 [examples/events.md](examples/events.md)。不要一次性把全部阶段事件喂入后关闭管道：这不能代替真实操作推进，输入结束会退出控制器。`180` 秒仅是安全到期时间，不是预测窗口；预测窗口始终是后续四个操作。

## 4. 验证

```sh
python3 scripts/verify_sources.py
make test
```

完整测试会编译真实 C 查表库，比较 C / JSON 结果，并验证原控制器行为。控制逻辑测试使用隔离的测试后端，不写真实 cgroup；测试通过不代表峰值性能收益已通过验收。

## 数据范围

当前查表只注册 GIMP。共 960 条记录，其中实测 192 条、模拟 768 条，24 条启动阶段缺页值缺失。原始数据来源及导入校验保留在 `data/`、`legacy_reference/`。总缺页次数仅用于分类，不能证明页面身份或页面复用。

`legacy_reference/verification.json` 是原查表提供者的历史验证记录；本次交付验证见 `VALIDATION.md`。此次新峰值对照尚未完成，不能据此宣称已防止峰值卡顿。

## 上传 GitHub

解压交付 ZIP，把本目录内的文件和子目录作为仓库内容上传，保留目录结构。忽略本地生成的 `build/`、`.parp-test-build/` 和 `__pycache__/`。

源码已有许可标识和原始出处保持不变；本次仅整理交付目录。
