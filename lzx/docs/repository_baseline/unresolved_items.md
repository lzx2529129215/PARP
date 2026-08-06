# 未解决问题

以下问题均保持OPEN，未通过推测关闭。

## UR-001
- question: monitor.py与online_monitor.py未来是否合并或长期双入口
- affected_paths: runtime_monitor/monitor.py, runtime_monitor/online_monitor.py
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 2
- required_user_decision: 确认产品正式入口策略
- safe_default: 保持monitor主入口与online兼容入口并存
- status: OPEN

## UR-002
- question: 部分checkpoint缺少完整训练commit、数据manifest或可重建输入
- affected_paths: operation_predictor/outputs/checkpoints
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 1
- required_user_decision: 确认旧模型是否仍承担兼容职责
- safe_default: 全部保留并禁止覆盖
- status: OPEN

## UR-003
- question: LSApp为何以内嵌Git仓库存在但不是submodule
- affected_paths: operation_predictor/data/raw/datasets/LSApp
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 1
- required_user_decision: 确认来源和未来版本策略
- safe_default: 保持独立Git边界且不修改九项变更
- status: OPEN

## UR-004
- question: cache_ext当前正式状态和历史修改来源
- affected_paths: cache_ext
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 1
- required_user_decision: 确认owner、上游和论文复现需求
- safe_default: 禁止移动和归档
- status: OPEN

## UR-005
- question: MGLRU-test v0-v4及mem/Linux多套内核树的长期角色
- affected_paths: MGLRU-test, mem/Linux
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 6
- required_user_decision: 确认canonical、fallback和archive版本
- safe_default: canonical仅登记v0正式树，不移动其他树
- status: OPEN

## UR-006
- question: Harmony正式工程是否还存在外部Windows工作区
- affected_paths: mem/Harmony
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 4
- required_user_decision: 确认现场源码和外部数据位置
- safe_default: 只保护当前可见资产
- status: OPEN

## UR-007
- question: 2698个UNKNOWN入口中哪些属于第一方正式工具
- affected_paths: outputs/repository_audit_20260717_175447/tables/entrypoints.csv
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 2
- required_user_decision: 按owner和运行证据确认
- safe_default: 全部视为不可删除
- status: OPEN

## UR-008
- question: app_vocab.json与app_vocab_duration.json及bundle副本的正式关系
- affected_paths: operation_predictor/data/vocab
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 3
- required_user_decision: 确认v2/v3兼容期限
- safe_default: v3 runtime使用duration vocab，旧vocab继续保留
- status: OPEN

## UR-009
- question: operation词表正式source of truth
- affected_paths: operation_predictor/data/vocab/op_vocab.json, configs/vocab
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 3
- required_user_decision: 确认消费者和版本
- safe_default: 不合并现有词表
- status: OPEN

## UR-010
- question: 用户态workload/state ID与内核ID映射的版本契约
- affected_paths: runtime_monitor/core/workload_classifier.py, MGLRU-test/v0/mglru_kernel_transfer/linux-hwe-6.17-6.17.0/mm/vmscan.c
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 3
- required_user_decision: 确认公共schema owner
- safe_default: 保持当前数值，不修改
- status: OPEN

## UR-011
- question: 哪些历史outputs不可重建
- affected_paths: outputs
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 1
- required_user_decision: 逐session确认设备、代码、配置和manifest
- safe_default: 默认不移动、不删除
- status: OPEN

## UR-012
- question: 哪些备份具有唯一内容
- affected_paths: MGLRU-test, outputs, mem
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 1
- required_user_decision: 对候选做owner审查和定向hash
- safe_default: 不归档任何备份
- status: OPEN

## UR-013
- question: Harmony非UI管线Smoke的唯一正式结果目录
- affected_paths: mem/Harmony/v6-Homeny
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 1
- required_user_decision: 确认历史执行结果是否在外部工作区
- safe_default: 保护当前HDC输出和56维工具
- status: OPEN

## UR-014
- question: 6.8.0-124-generic vmlinuz普通用户不可读，未取得SHA256
- affected_paths: /boot/vmlinuz-6.8.0-124-generic
- current_evidence: 源审计和Phase0定向检查
- possible_answers: 需要owner确认
- blocking_phase: Phase 0 review
- required_user_decision: 由管理员只读校验或确认不作为首选回退
- safe_default: 以bindfix作为当前明确回退
- status: OPEN
