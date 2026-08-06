# Phase 0校验

- 结果：`PASS`
- JSON注册表：8个，均可由Python标准库解析
- 路径检查：82项，其中存在79项、明确未决3项
- SHA-256：所有非空SHA字段均为64位小写十六进制
- 唯一ID、枚举值、跨表引用、保护原因和历史测试标记：通过
- 占位符与敏感信息扫描：未发现待办占位标记、私钥或敏感赋值
- Phase 0 manifest：`outputs/repository_phase0_20260717_195813/validation/phase0_manifest.sha256`，最终封存校验为`PASS`

## 已解释警告

- model:lsapp_app_lstm_v2: 数据集引用 'lsapp_v2_processed'尚未解析，已由provenance状态明确保留
- model:app_lstm_v2_original: 数据集引用 'original_v2_processed_unresolved'尚未解析，已由provenance状态明确保留
- kernel:ubuntu_generic_recovery: vmlinuz存在但当前用户不可读，SHA保留为空并进入未决清单
