# 环境与权限

需要 Linux cgroup v2、可访问的 `/proc` 和 `/sys/fs/cgroup`、构建工具以及能加载 BPF 的 sudo/root 权限。运行 `python3 scripts/check_environment.py` 获取主机特定结果。

本仓库的 eBPF 程序不是 CO-RE；kprobe 名称会随内核而变化。环境检查将 hook 不可见标为 WARN，实际加载仍应以 collector 的 stderr 和返回值为准。

WPS 自动化是 optional：它需要 `xdotool`、WPS、活动 GUI session 和使用者提供的样本路径。不要从本仓库复制其他机器的 DISPLAY、UID 或 Xauthority 值。
