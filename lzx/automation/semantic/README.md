# 语义自动化框架

本目录保存可复用的高层 operation、场景、窗口 Profile、素材示例和编译器支持文件。

`operations/` 只描述业务语义和低层 action 模板；`scenarios/` 只引用 operation，不直接复制 UI 细节。编译后生成的 `trace_marker` 用于时间区间对齐。`requested_operation` 不是 workload 标签，真实 workload 仍由 Runtime Monitor 的 cgroup classifier 产生。

默认仅允许 `NONE` 和 `LOCAL_ONLY` 操作。真实消息、发布、关注、收藏和在线文档写入必须显式开启安全开关，并提供本地测试账号及 allowlist；这些信息不能提交到仓库。

校准坐标时使用 `automation/app_automation.py --calibration-only`。窗口不匹配时不得盲点。
