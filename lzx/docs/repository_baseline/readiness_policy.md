# Readiness政策

禁止使用单个ready=true代表整个系统完成。每个状态必须携带value、reason、evidence、blocking_items和last_validated_at。

```json
{
  "source_ready": {
    "value": "true",
    "reason": "存在当前基线证据",
    "evidence": [
      "test_baseline.json",
      "model_registry.json"
    ],
    "blocking_items": [],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "unit_test_ready": {
    "value": "true",
    "reason": "存在当前基线证据",
    "evidence": [
      "test_baseline.json",
      "model_registry.json"
    ],
    "blocking_items": [],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "offline_pipeline_ready": {
    "value": "partial",
    "reason": "存在历史证据但本次未重跑设备/内核链",
    "evidence": [
      "test_baseline.json"
    ],
    "blocking_items": [
      "需按目标环境重新验证"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "device_collector_ready": {
    "value": "partial",
    "reason": "存在历史证据但本次未重跑设备/内核链",
    "evidence": [
      "test_baseline.json"
    ],
    "blocking_items": [
      "需按目标环境重新验证"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "device_ui_ready": {
    "value": "false",
    "reason": "未进行本阶段对应运行验证",
    "evidence": [],
    "blocking_items": [
      "需要分层验证和owner确认"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "single_stage_ready": {
    "value": "partial",
    "reason": "存在历史证据但本次未重跑设备/内核链",
    "evidence": [
      "test_baseline.json"
    ],
    "blocking_items": [
      "需按目标环境重新验证"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "full_session_ready": {
    "value": "false",
    "reason": "未进行本阶段对应运行验证",
    "evidence": [],
    "blocking_items": [
      "需要分层验证和owner确认"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "cross_trial_ready": {
    "value": "false",
    "reason": "未进行本阶段对应运行验证",
    "evidence": [],
    "blocking_items": [
      "需要分层验证和owner确认"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "model_training_ready": {
    "value": "false",
    "reason": "未进行本阶段对应运行验证",
    "evidence": [],
    "blocking_items": [
      "需要分层验证和owner确认"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "model_runtime_ready": {
    "value": "true",
    "reason": "存在当前基线证据",
    "evidence": [
      "test_baseline.json",
      "model_registry.json"
    ],
    "blocking_items": [],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "kernel_build_ready": {
    "value": "partial",
    "reason": "存在历史证据但本次未重跑设备/内核链",
    "evidence": [
      "test_baseline.json"
    ],
    "blocking_items": [
      "需按目标环境重新验证"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "kernel_runtime_ready": {
    "value": "partial",
    "reason": "存在历史证据但本次未重跑设备/内核链",
    "evidence": [
      "test_baseline.json"
    ],
    "blocking_items": [
      "需按目标环境重新验证"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "observe_ready": {
    "value": "true",
    "reason": "存在当前基线证据",
    "evidence": [
      "test_baseline.json",
      "kernel_registry.json"
    ],
    "blocking_items": [],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "recognition_ready": {
    "value": "false",
    "reason": "未进行本阶段对应运行验证",
    "evidence": [],
    "blocking_items": [
      "需要分层验证和owner确认"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  },
  "apply_ready": {
    "value": "false",
    "reason": "observe-only基线，禁止Apply",
    "evidence": [
      "kernel_registry.json"
    ],
    "blocking_items": [
      "未批准真实回收策略修改"
    ],
    "last_validated_at": "2026-07-17T20:13:11.560759+08:00"
  }
}
```
