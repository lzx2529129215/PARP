# operation_predictor 目录说明

# operation\_predictor 目录说明

`operation_predictor` 是用于“应用/操作预测”相关实验的数据与脚本目录。当前内容主要围绕三类信息组织：

1. **应用、操作、用户群体等离散字段的词表编码；**
2. **已经处理好的样本数据；**
3. **用于环境配置、数据采集与结果分析的辅助脚本。**

## 当前目录结构

```
operation_predictor/
├── data/
│   ├── processed/
│   │   ├── app_samples.pkl
│   │   └── op_samples.pkl
│   └── vocab/
│       ├── app_vocab.json
│       ├── op_vocab.json
│       └── user_group_vocab.json
├── images/
│   └── README/
│       └── 1780905223447.png
├── scripts/
│   ├── device/
│   │   ├── setup_env.sh
│   │   └── collect.sh
│   ├── analysis/
│   │   └── analyze_memory.py
│   └── pipeline/
│       └── run_first_stage.sh
├── requirements.txt
└── README.md
```

## 文件夹介绍

### `data/`

`data/` 是本模块的数据总目录，用于存放预测任务所需的输入数据、中间数据和编码文件。它不直接区分某一个具体模型，而是按照数据状态继续划分为 `vocab/` 和 `processed/` 两类子目录：

* `vocab/` 保存词表和类别编码；
* `processed/` 保存已经预处理完成、可被程序直接读取的样本数据。

**这种结构可以把“字段如何编码”和“样本如何使用”分开，方便后续扩展新的应用、操作类型或用户分组。**

### `data/vocab/`

`data/vocab/` 用于保存离散字段的词表映射文件。预测模型通常不能直接处理中文应用名、操作名或用户群体名称，因此需要先把这些文本标签转换为数字编号。

**当前包含以下词表：**

* `app_vocab.json`：应用词表，记录应用名称到编号的映射，例如 WPS、飞书、腾讯 QQ、斗鱼、剪映、浏览器、视频类应用、游戏类应用等。
* `op_vocab.json`：操作词表，按应用或应用类型组织操作名称，例如打开应用、点击窗口、发送消息、切换标签页等。
* `user_group_vocab.json`：用户群体词表，记录学生、程序员、工程师、网红等用户类型到编号的映射。

**该目录的作用是统一编码规则，避免不同脚本或不同阶段对同一类字段使用不一致的编号。**

### `data/processed/`

`data/processed/` 用于保存已经完成预处理的样本文件。这里的数据通常已经经过清洗、编码或格式转换，可以直接供训练、统计分析、验证或预测流程加载。

**当前包含以下文件：**

* `app_samples.pkl`：应用维度的样本数据，通常用于和应用选择、应用使用模式或应用预测相关的任务。
* `op_samples.pkl`：操作维度的样本数据，通常用于和用户操作序列、下一步操作预测或操作行为分析相关的任务。
  * **每个操作都定义了：**
    * <**PAD**>**：用于补齐历史操作序列长度**
    * <**UNK**>**：用于处理未收录的新操作**

`.pkl` 文件是 Python pickle 格式，适合保存处理后的 Python 对象。使用时需要注意生成脚本、字段结构和 Python 环境的一致性。

### `images/`

`images/` 是图片资源总目录，用于集中保存文档或实验说明中引用的图片。将图片资源从代码和数据目录中分离出来，可以让项目结构更清晰，也方便 README、实验报告或说明文档统一引用。

**后续如果需要保存模型结构图、流程图、数据示意图、运行截图或实验结果截图，可以继续放在该目录下，并按照用途建立子目录。**

### `images/README/`

`images/README/` 专门用于存放 README 文档引用的图片资源。当前包含：

* `1780905223447.png`：README 相关图片素材。

**如果 README 中增加新的插图，建议继续放在该目录中，避免和其他实验图片混在一起。**

### `scripts/`

`scripts/` 是脚本目录，用于保存本模块相关的自动化脚本。当前脚本主要与环境初始化、设备采集和内存数据分析有关。

**当前包含以下脚本：**

* `setup_env.sh`：环境初始化脚本，用于配置 `hdc`、交叉编译工具链或其他运行所需路径。按照项目约定，执行 hdc 相关命令前应先加载该脚本。
* `collect.sh`：采集脚本，用于执行目标进程的数据采集流程，包括编译、推送、运行采集程序以及拉回结果等步骤。
* `analyze_memory.py`：分析脚本，用于对采集得到的内存快照或 CSV 结果进行统计、对比和整理。

**该目录可以视为数据生成与分析流程的入口，后续如果增加样本构建、特征提取、模型训练或预测脚本，也建议继续放在这里。**

## 根目录文件说明

**虽然以下内容不是文件夹，但它们位于 **`operation_predictor` 根目录，对使用本模块也很重要：

* `requirements.txt`：Python 依赖说明文件，用于记录运行脚本或实验所需的第三方库。当前文件为空，说明目前脚本可能主要依赖 Python 标准库，或依赖尚未补充。
* `README.md`：当前说明文档，用于解释 `operation_predictor` 的目录结构和各部分职责。

## 建议的数据使用流程

```
词表文件 data/vocab/
        ↓
样本构建或预处理脚本
        ↓
处理后样本 data/processed/
        ↓
训练、分析或预测流程
```

**在扩展该目录时，建议保持以下约定：**

1. **新增类别字段时，优先在 **`data/vocab/` 中维护统一词表；
2. **新增处理后的样本时，放入 **`data/processed/`，并说明生成方式；
3. **新增自动化流程时，放入 **`scripts/`，避免脚本散落在根目录；
4. **README 中引用的图片放入 **`images/README/`，其他图片按用途另建子目录。

## 新增实验组文件说明

```
lzx/operation_predictor/
├── src/
│   ├── models/
│   │   ├── app_markov.py
│   │   ├── op_markov.py
│   │   ├── op_parametric.py        # 新增：参数化回归实验组
│   │   ├── op_gam.py               # 新增：GAM 实验组
│   │   ├── op_quantile.py          # 新增：神经分位数回归实验组
│   │   ├── op_scnn.py              # 新增：SCNN 实验组
│   │   └── op_mogp.py              # 新增：MOGP 实验组
│   │
│   └── train/
│       ├── train_app_markov.py
│       ├── train_op_markov.py
│       ├── train_op_parametric.py  # 新增：参数化回归训练入口
│       ├── train_op_gam.py         # 新增：GAM 训练入口
│       ├── train_op_quantile.py    # 新增：分位数回归训练入口
│       ├── train_op_scnn.py        # 新增：SCNN 训练入口
│       └── train_op_mogp.py        # 新增：MOGP 训练入口
│
└── outputs/
    └── results/
        ├── app_markov_results.csv
        ├── op_markov_results.csv
        ├── op_parametric_results.csv  # 新增：参数化回归结果
        ├── op_gam_results.csv         # 新增：GAM 结果
        ├── op_quantile_results.csv    # 新增：分位数回归结果
        ├── op_scnn_results.csv        # 新增：SCNN 结果
        └── op_mogp_results.csv        # 新增：MOGP 结果
```

## 新增应用内操作预测实验组

**本项目原有 **`op_markov.py` / `train_op_markov.py` 作为应用内操作预测的 Markov baseline。新增的实验组均用于同一任务：

```
输入：某个应用内最近 N 次操作 history_ops
输出：下一步操作的 Top-K 候选
评估：topk_accuracy、MRR
数据：data/processed/train_op.pkl、data/processed/test_op.pkl
```

**这些实验组和 Markov 是并列关系，不会调用 **`OpMarkovModel`。每个实验组都有独立模型文件和独立训练脚本。

### 文件结构

```
lzx/operation_predictor/
├── src/
│   ├── models/
│   │   ├── op_markov.py          # 对照组：Markov baseline
│   │   ├── op_parametric.py      # 实验组：参数化回归
│   │   ├── op_gam.py             # 实验组：广义加性模型 GAM
│   │   ├── op_quantile.py        # 实验组：神经分位数回归
│   │   ├── op_scnn.py            # 实验组：Shape-Constrained Neural Networks
│   │   └── op_mogp.py            # 实验组：多输出高斯过程 MOGP
│   └── train/
│       ├── train_op_markov.py
│       ├── train_op_parametric.py
│       ├── train_op_gam.py
│       ├── train_op_quantile.py
│       ├── train_op_scnn.py
│       └── train_op_mogp.py
└── outputs/
    └── results/
        ├── op_markov_results.csv
        ├── op_parametric_results.csv
        ├── op_gam_results.csv
        ├── op_quantile_results.csv
        ├── op_scnn_results.csv
        └── op_mogp_results.csv
```

### 各方法逻辑


| **方法**            | **模型文件**                  | **核心逻辑**                                                                                                         |
| ------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| **Markov baseline** | `src/models/op_markov.py`     | **统计应用内历史操作上下文到下一步操作的转移频次，支持 1-4 阶，作为对照组。**                                        |
| **参数化回归**      | `src/models/op_parametric.py` | **对候选操作构造线性特征并学习权重，例如候选操作、历史长度、最近操作、候选是否在历史中出现等，然后按线性分数排序。** |
| **GAM**             | `src/models/op_gam.py`        | **将候选操作得分拆成多个可加分量，例如候选先验、历史长度分量、最近操作分量、重复操作分量，各分量相加得到最终分数。** |
| **神经分位数回归**  | `src/models/op_quantile.py`   | **使用哈希稀疏特征模拟轻量神经表示，为多个分位数学习权重，预测时用指定分位数分数排序候选操作。**                     |
| **SCNN**            | `src/models/op_scnn.py`       | **使用带形状约束的打分器，对“候选操作重复出现”等特征施加非负权重约束，使重复证据增加时候选分数不会下降。**         |
| **MOGP**            | `src/models/op_mogp.py`       | **将每个候选操作看作一个输出，结合上下文原型的 RBF 相似度和操作之间的协同出现统计，为多个输出候选同时打分。**        |

**说明：新增模型均为标准库实现，目的是在现有数据格式下提供可跑通、可对比的实验组。它们不依赖外部机器学习库，也不调用 Markov。**

### 使用方法

**进入项目目录：**

```
cd lzx/operation_predictor
```

**如果已经有处理好的数据，可直接运行各实验组：**

```
python src/train/train_op_markov.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --order 4 \
  --top-k 1 3 5 \
  --output outputs/results/op_markov_results.csv
```

```
python src/train/train_op_parametric.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_parametric_results.csv
```

```
python src/train/train_op_gam.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_gam_results.csv
```

```
python src/train/train_op_quantile.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_quantile_results.csv
```

```
python src/train/train_op_scnn.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_scnn_results.csv
```

```
python src/train/train_op_mogp.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_mogp_results.csv
```

### 从原始 op\_events.csv 重新生成数据

**如果要使用新的应用内操作日志，先准备：**

```
data/raw/op_events.csv
```

**字段格式：**

```
user_id,timestamp,app,operation,user_group
```

**然后重新构造样本、切分数据、训练模型：**

```
python src/data/build_op_dataset.py \
  --input data/raw/op_events.csv \
  --op-vocab data/vocab/op_vocab.json \
  --group-vocab data/vocab/user_group_vocab.json \
  --output data/processed/op_samples.pkl \
  --history-len 4
```

```
python src/data/split_dataset.py \
  --input data/processed/op_samples.pkl \
  --task op \
  --output-dir data/processed
```

**之后再运行上面的任一 **`train_op_*.py` 脚本即可。

### 输出结果

**所有应用内操作预测实验组输出相同格式的 CSV：**

```
app,model,order,k,topk_accuracy,mrr,num_samples
```

**字段含义：**


| **字段**        | **含义**                                             |
| --------------- | ---------------------------------------------------- |
| `app`           | **应用名。**                                         |
| `model`         | **模型名称，例如**`op_markov`、`op_gam`、`op_scnn`。 |
| `order`         | **Markov 阶数；非 Markov 实验组为空。**              |
| `k`             | **Top-K 的 K 值。**                                  |
| `topk_accuracy` | **真实下一步操作是否出现在 Top-K 预测中。**          |
| `mrr`           | **Mean Reciprocal Rank，真实操作排得越靠前越高。**   |
| `num_samples`   | **参与评估的测试样本数。**                           |

**对比时建议以 **`op_markov_results.csv` 作为 baseline，对照其他实验组的同应用、同 `k` 指标。

# operation\_predictor 目录说明

`operation_predictor` 是用于“应用/操作预测”相关实验的数据与脚本目录。当前内容主要围绕三类信息组织：

1. **应用、操作、用户群体等离散字段的词表编码；**
2. **已经处理好的样本数据；**
3. **用于环境配置、数据采集与结果分析的辅助脚本。**

## 当前目录结构

```
operation_predictor/
├── data/
│   ├── processed/
│   │   ├── app_samples.pkl
│   │   └── op_samples.pkl
│   └── vocab/
│       ├── app_vocab.json
│       ├── op_vocab.json
│       └── user_group_vocab.json
├── images/
│   └── README/
│       └── 1780905223447.png
├── scripts/
│   ├── device/
│   │   ├── setup_env.sh
│   │   └── collect.sh
│   ├── analysis/
│   │   └── analyze_memory.py
│   └── pipeline/
│       └── run_first_stage.sh
├── requirements.txt
└── README.md
```

## 文件夹介绍

### `data/`

`data/` 是本模块的数据总目录，用于存放预测任务所需的输入数据、中间数据和编码文件。它不直接区分某一个具体模型，而是按照数据状态继续划分为 `vocab/` 和 `processed/` 两类子目录：

* `vocab/` 保存词表和类别编码；
* `processed/` 保存已经预处理完成、可被程序直接读取的样本数据。

**这种结构可以把“字段如何编码”和“样本如何使用”分开，方便后续扩展新的应用、操作类型或用户分组。**

### `data/vocab/`

`data/vocab/` 用于保存离散字段的词表映射文件。预测模型通常不能直接处理中文应用名、操作名或用户群体名称，因此需要先把这些文本标签转换为数字编号。

**当前包含以下词表：**

* `app_vocab.json`：应用词表，记录应用名称到编号的映射，例如 WPS、飞书、腾讯 QQ、斗鱼、剪映、浏览器、视频类应用、游戏类应用等。
* `op_vocab.json`：操作词表，按应用或应用类型组织操作名称，例如打开应用、点击窗口、发送消息、切换标签页等。
* `user_group_vocab.json`：用户群体词表，记录学生、程序员、工程师、网红等用户类型到编号的映射。

**该目录的作用是统一编码规则，避免不同脚本或不同阶段对同一类字段使用不一致的编号。**

### `data/processed/`

`data/processed/` 用于保存已经完成预处理的样本文件。这里的数据通常已经经过清洗、编码或格式转换，可以直接供训练、统计分析、验证或预测流程加载。

**当前包含以下文件：**

* `app_samples.pkl`：应用维度的样本数据，通常用于和应用选择、应用使用模式或应用预测相关的任务。
* `op_samples.pkl`：操作维度的样本数据，通常用于和用户操作序列、下一步操作预测或操作行为分析相关的任务。
  * **每个操作都定义了：**
    * <**PAD**>**：用于补齐历史操作序列长度**
    * <**UNK**>**：用于处理未收录的新操作**

`.pkl` 文件是 Python pickle 格式，适合保存处理后的 Python 对象。使用时需要注意生成脚本、字段结构和 Python 环境的一致性。

### `images/`

`images/` 是图片资源总目录，用于集中保存文档或实验说明中引用的图片。将图片资源从代码和数据目录中分离出来，可以让项目结构更清晰，也方便 README、实验报告或说明文档统一引用。

**后续如果需要保存模型结构图、流程图、数据示意图、运行截图或实验结果截图，可以继续放在该目录下，并按照用途建立子目录。**

### `images/README/`

`images/README/` 专门用于存放 README 文档引用的图片资源。当前包含：

* `1780905223447.png`：README 相关图片素材。

**如果 README 中增加新的插图，建议继续放在该目录中，避免和其他实验图片混在一起。**

### `scripts/`

`scripts/` 是脚本目录，用于保存本模块相关的自动化脚本。当前脚本主要与环境初始化、设备采集和内存数据分析有关。

**当前包含以下脚本：**

* `setup_env.sh`：环境初始化脚本，用于配置 `hdc`、交叉编译工具链或其他运行所需路径。按照项目约定，执行 hdc 相关命令前应先加载该脚本。
* `collect.sh`：采集脚本，用于执行目标进程的数据采集流程，包括编译、推送、运行采集程序以及拉回结果等步骤。
* `analyze_memory.py`：分析脚本，用于对采集得到的内存快照或 CSV 结果进行统计、对比和整理。

**该目录可以视为数据生成与分析流程的入口，后续如果增加样本构建、特征提取、模型训练或预测脚本，也建议继续放在这里。**

## 根目录文件说明

**虽然以下内容不是文件夹，但它们位于 **`operation_predictor` 根目录，对使用本模块也很重要：

* `requirements.txt`：Python 依赖说明文件，用于记录运行脚本或实验所需的第三方库。当前文件为空，说明目前脚本可能主要依赖 Python 标准库，或依赖尚未补充。
* `README.md`：当前说明文档，用于解释 `operation_predictor` 的目录结构和各部分职责。

## 建议的数据使用流程

```
词表文件 data/vocab/
        ↓
样本构建或预处理脚本
        ↓
处理后样本 data/processed/
        ↓
训练、分析或预测流程
```

**在扩展该目录时，建议保持以下约定：**

1. **新增类别字段时，优先在 **`data/vocab/` 中维护统一词表；
2. **新增处理后的样本时，放入 **`data/processed/`，并说明生成方式；
3. **新增自动化流程时，放入 **`scripts/`，避免脚本散落在根目录；
4. **README 中引用的图片放入 **`images/README/`，其他图片按用途另建子目录。

## 新增实验组文件说明

```
lzx/operation_predictor/
├── src/
│   ├── models/
│   │   ├── app_markov.py
│   │   ├── op_markov.py
│   │   ├── op_parametric.py        # 新增：参数化回归实验组
│   │   ├── op_gam.py               # 新增：GAM 实验组
│   │   ├── op_quantile.py          # 新增：神经分位数回归实验组
│   │   ├── op_scnn.py              # 新增：SCNN 实验组
│   │   └── op_mogp.py              # 新增：MOGP 实验组
│   │
│   └── train/
│       ├── train_app_markov.py
│       ├── train_op_markov.py
│       ├── train_op_parametric.py  # 新增：参数化回归训练入口
│       ├── train_op_gam.py         # 新增：GAM 训练入口
│       ├── train_op_quantile.py    # 新增：分位数回归训练入口
│       ├── train_op_scnn.py        # 新增：SCNN 训练入口
│       └── train_op_mogp.py        # 新增：MOGP 训练入口
│
└── outputs/
    └── results/
        ├── app_markov_results.csv
        ├── op_markov_results.csv
        ├── op_parametric_results.csv  # 新增：参数化回归结果
        ├── op_gam_results.csv         # 新增：GAM 结果
        ├── op_quantile_results.csv    # 新增：分位数回归结果
        ├── op_scnn_results.csv        # 新增：SCNN 结果
        └── op_mogp_results.csv        # 新增：MOGP 结果
```

## 新增应用内操作预测实验组

**本项目原有 **`op_markov.py` / `train_op_markov.py` 作为应用内操作预测的 Markov baseline。新增的实验组均用于同一任务：

```
输入：某个应用内最近 N 次操作 history_ops
输出：下一步操作的 Top-K 候选
评估：topk_accuracy、MRR
数据：data/processed/train_op.pkl、data/processed/test_op.pkl
```

**这些实验组和 Markov 是并列关系，不会调用 **`OpMarkovModel`。每个实验组都有独立模型文件和独立训练脚本。

### 文件结构

```
lzx/operation_predictor/
├── src/
│   ├── models/
│   │   ├── op_markov.py          # 对照组：Markov baseline
│   │   ├── op_parametric.py      # 实验组：参数化回归
│   │   ├── op_gam.py             # 实验组：广义加性模型 GAM
│   │   ├── op_quantile.py        # 实验组：神经分位数回归
│   │   ├── op_scnn.py            # 实验组：Shape-Constrained Neural Networks
│   │   └── op_mogp.py            # 实验组：多输出高斯过程 MOGP
│   └── train/
│       ├── train_op_markov.py
│       ├── train_op_parametric.py
│       ├── train_op_gam.py
│       ├── train_op_quantile.py
│       ├── train_op_scnn.py
│       └── train_op_mogp.py
└── outputs/
    └── results/
        ├── op_markov_results.csv
        ├── op_parametric_results.csv
        ├── op_gam_results.csv
        ├── op_quantile_results.csv
        ├── op_scnn_results.csv
        └── op_mogp_results.csv
```

### 各方法逻辑


| **方法**            | **模型文件**                  | **核心逻辑**                                                                                                         |
| ------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| **Markov baseline** | `src/models/op_markov.py`     | **统计应用内历史操作上下文到下一步操作的转移频次，支持 1-4 阶，作为对照组。**                                        |
| **参数化回归**      | `src/models/op_parametric.py` | **对候选操作构造线性特征并学习权重，例如候选操作、历史长度、最近操作、候选是否在历史中出现等，然后按线性分数排序。** |
| **GAM**             | `src/models/op_gam.py`        | **将候选操作得分拆成多个可加分量，例如候选先验、历史长度分量、最近操作分量、重复操作分量，各分量相加得到最终分数。** |
| **神经分位数回归**  | `src/models/op_quantile.py`   | **使用哈希稀疏特征模拟轻量神经表示，为多个分位数学习权重，预测时用指定分位数分数排序候选操作。**                     |
| **SCNN**            | `src/models/op_scnn.py`       | **使用带形状约束的打分器，对“候选操作重复出现”等特征施加非负权重约束，使重复证据增加时候选分数不会下降。**         |
| **MOGP**            | `src/models/op_mogp.py`       | **将每个候选操作看作一个输出，结合上下文原型的 RBF 相似度和操作之间的协同出现统计，为多个输出候选同时打分。**        |

**说明：新增模型均为标准库实现，目的是在现有数据格式下提供可跑通、可对比的实验组。它们不依赖外部机器学习库，也不调用 Markov。**

### 使用方法

**进入项目目录：**

```
cd lzx/operation_predictor
```

**如果已经有处理好的数据，可直接运行各实验组：**

```
python src/train/train_op_markov.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --order 4 \
  --top-k 1 3 5 \
  --output outputs/results/op_markov_results.csv
```

```
python src/train/train_op_parametric.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_parametric_results.csv
```

```
python src/train/train_op_gam.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_gam_results.csv
```

```
python src/train/train_op_quantile.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_quantile_results.csv
```

```
python src/train/train_op_scnn.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_scnn_results.csv
```

```
python src/train/train_op_mogp.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --op-vocab data/vocab/op_vocab.json \
  --top-k 1 3 5 \
  --output outputs/results/op_mogp_results.csv
```

### 从原始 op\_events.csv 重新生成数据

**如果要使用新的应用内操作日志，先准备：**

```
data/raw/op_events.csv
```

**字段格式：**

```
user_id,timestamp,app,operation,user_group
```

**然后重新构造样本、切分数据、训练模型：**

```
python src/data/build_op_dataset.py \
  --input data/raw/op_events.csv \
  --op-vocab data/vocab/op_vocab.json \
  --group-vocab data/vocab/user_group_vocab.json \
  --output data/processed/op_samples.pkl \
  --history-len 4
```

```
python src/data/split_dataset.py \
  --input data/processed/op_samples.pkl \
  --task op \
  --output-dir data/processed
```

**之后再运行上面的任一 **`train_op_*.py` 脚本即可。

### 输出结果

**所有应用内操作预测实验组输出相同格式的 CSV：**

```
app,model,order,k,topk_accuracy,mrr,num_samples
```

**字段含义：**


| **字段**        | **含义**                                             |
| --------------- | ---------------------------------------------------- |
| `app`           | **应用名。**                                         |
| `model`         | **模型名称，例如**`op_markov`、`op_gam`、`op_scnn`。 |
| `order`         | **Markov 阶数；非 Markov 实验组为空。**              |
| `k`             | **Top-K 的 K 值。**                                  |
| `topk_accuracy` | **真实下一步操作是否出现在 Top-K 预测中。**          |
| `mrr`           | **Mean Reciprocal Rank，真实操作排得越靠前越高。**   |
| `num_samples`   | **参与评估的测试样本数。**                           |

**对比时建议以 **`op_markov_results.csv` 作为 baseline，对照其他实验组的同应用、同 `k` 指标。

`operation_predictor` 是用于“应用/操作预测”相关实验的数据与脚本目录。除了原有的词表与已处理样本外，仓库中包含了完整的数据预处理流水线、会话级 `opened_apps` 构造、基于会话训练的 Markov 基线以及用于生成 v1 输入的脚本。

## 当前目录要点（已同步）

主要子目录和关键文件：

- `data/vocab/`：保存 `app_vocab.json`, `op_vocab.json`, `user_group_vocab.json` 等词表。
- `data/datasets/LSApp/`：原始与中间数据集目录（例如 `lsapp.tsv`, `after_mapping/`, `models/` 等）。
- `data/raw/`：用于放置清洗后供 `build_app_dataset.py` 读取的事件文件，例如 `data/raw/app_events_for_build.tsv` 和对应的 `.csv`。
- `data/processed/`：保存训练用的最终样本，如 `app_samples.pkl`、`op_samples.pkl`。

另外存在若干实用脚本用于数据管道：

- `scripts/decompress_raw_gz.py`：解压合并后的 `.gz` 文件到 `data/raw`。
- `scripts/clean_tsv_remove_nuls.py`：清除 TSV 中的 NUL 字符并写出 clean 文件。
- `scripts/prepare_events_for_build.py`：从合并事件文件中抽取并过滤出 `user_id,timestamp,foreground_app,opened_apps,user_group` 等字段，写入 `data/raw/app_events_for_build.tsv`。
- `scripts/tsv_to_csv.py`：把准备好的 TSV 转为 CSV（供部分脚本使用）。
- `src/data/build_app_dataset.py`：将事件 CSV/TSV 构造为 v1 所需的 `app_samples.pkl`（可设置 history 长度与 horizons）。
- `src/data/split_dataset.py`：对生成的 `app_samples.pkl` 做训练/验证/测试拆分。
- `scripts/train_markov_from_sessions.py`：基于 `session_opened_apps.tsv` 训练一阶 Markov，并把模型保存到 `data/datasets/LSApp/models/`。

## 推荐的快速复现命令

在 `lzx/operation_predictor` 根目录下，可以按下面步骤复现数据准备流程（示例）：

```bash
cd lzx/operation_predictor
# 1) 解压并合并（如果有 .gz 版本）
python scripts/decompress_raw_gz.py

# 2) 清除 TSV 文件中的 NUL 字符
python scripts/clean_tsv_remove_nuls.py

# 3) 生成会话级 opened_apps 并合并回事件文件（已在仓库脚本中实现）
python scripts/generate_session_opened_apps.py
python scripts/merge_opened_apps_into_lsapp.py

# 4) 从合并后的事件中准备 build 输入（筛选字段、默认用户分组等）
python scripts/prepare_events_for_build.py

# 5) TSV -> CSV（部分工具需要 CSV 格式）
python scripts/tsv_to_csv.py

# 6) 构建 app_samples.pkl（可指定 history len 与 horizons）
python src/data/build_app_dataset.py \
  --input data/raw/app_events_for_build.csv \
  --app-vocab data/vocab/app_vocab.json \
  --group-vocab data/vocab/user_group_vocab.json \
  --output data/processed/app_samples.pkl \
  --history-len 6 --horizons 3 5 10

# 7) 拆分为 train/val/test
python src/data/split_dataset.py --input data/processed/app_samples.pkl --task app --output-dir data/processed

# 8) （可选）训练并保存 Markov 基线模型（会在 data/datasets/LSApp/models/ 下生成）
python scripts/train_markov_from_sessions.py
```

## 运行环境与依赖

- Python 3.8+（仓库多数脚本基于标准库，使用 conda/miniconda 推荐）
- 推荐先创建并激活虚拟环境，然后安装项目依赖（`requirements.txt` 目前为空，按需安装 `torch` 等库用于 v2 模型训练）。

示例（conda）：

```bash
conda create -n op_pred python=3.9 -y
conda activate op_pred
# 安装必要库，例如：
pip install -r requirements.txt
pip install torch
```

## 输出位置说明

- `data/processed/app_samples.pkl`：v1 训练样本（已经在仓库中生成的示例文件）。
- `data/datasets/LSApp/models/markov_model_v1.json`：基于会话训练的 Markov 模型输出。

## 贡献与扩展建议

1. 将新增的数据处理脚本放在 `scripts/` 下并在 README 中记录用途与输出路径。
2. 若增加新的词表或用户分组，先在 `data/vocab/` 中登记并版本化。
3. 把复杂/慢速步骤（如大文件处理）拆成小工具并添加参数以便调试。

如果你希望我把 README 中某段内容进一步展开为操作手册或把命令封装进 Makefile/脚本，我可以继续帮你实现。
