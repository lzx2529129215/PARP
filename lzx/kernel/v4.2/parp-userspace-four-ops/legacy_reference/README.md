# 查表函数：按应用返回后续4个操作的WSS和缺页次数

当前版本为API v3：末尾不足四步时，保留已有结果，其余位置补-1。请整体替换头文件和实现，重新编译；输出必须使用有符号64位数组。

## 函数输入

- app_name：应用名称，目前注册gimp；支持gimp/GIMP，未注册应用返回不支持，不会误查GIMP。
- sequence_num：该应用的序列号；当前GIMP为1～30。
- current_op_id：当前实际操作编号；当前GIMP为1～8，不是第一个目标操作。
- pressure_gib：压力档，单位GiB，只接受0、8、12、14；不是memory.max−RSS。

## 函数输出

- wss_bytes[4]：未来4个操作阶段各自的总WSS，单位bytes。
- pagefault[4]：未来4个操作阶段各自的总缺页次数，来自原表“总缺页次数”。

下标0～3分别对应current_op_id+1、+2、+3、+4。不相加，不按每5秒解释，不返回匿名/文件分项。

## 函数接口

```c
int get_wss_predictor(const char *app_name,
                      wss_pred_u32 sequence_num,
                      wss_pred_u32 current_op_id,
                      wss_pred_u64 pressure_gib,
                      wss_pred_i64 wss_bytes[4],
                      wss_pred_i64 pagefault[4]);
```

类型由get_wss_predictor.h提供：输入整数为uint32_t/uint64_t，输出为int64_t；内核对应u32/u64及s64。

## 调用示例

```c
#include "get_wss_predictor.h"

wss_pred_i64 wss[4];
wss_pred_i64 pagefault[4];
int ret;

ret = get_wss_predictor("gimp", 1, 1, 0, wss, pagefault);
if (ret == WSS_PRED_OK) {
    /* wss: 289120256, 485830656, 475205632, 553238528 bytes. */
    /* pagefault: 64992, 120144, 25773, 45476. */
} else {
    /* All array elements are unchanged. Do not use stale values. */
}
```

数组名会自动转换为指针，调用时传wss、pagefault，不传&wss、&pagefault。必须真正分配两个长度至少为4、可写且互不重叠的数组，不能把单个变量的地址传进来，也不能使用无符号输出数组。

函数只输出8个目标值，int返回码用于报告是否成功，没有额外预测字典。有效查询写入全部8个位置；超出序列末尾的位置补-1。内部记录缺失或数值非法等真正错误仍不修改两个数组。ret为0只表示查询成功，不保证四步都有有效数据；使用前逐个检查值是否非负。不要把-1转换为无符号数或作为回收目标。

## 返回码

|值|名称|含义|
|---|---|---|
|0|WSS_PRED_OK|查询成功；尾部可能含-1|
|-1|WSS_PRED_INVALID_ARGUMENT|参数、压力、空输出或重叠输出非法|
|-2|WSS_PRED_NOT_FOUND|当前序列、压力、操作记录不存在|
|-4|WSS_PRED_MISSING_FUTURE|预期未来四步内部记录缺失|
|-5|WSS_PRED_DATA_UNAVAILABLE|未来目标缺失、非法或无法无损表示为有符号64位整数|
|-6|WSS_PRED_UNSUPPORTED_APP|应用未注册|

返回码为本接口自定义值，不是Linux errno。必须检查ret==0，不能忽略失败后继续用上次结果。

当前每条GIMP轨迹8个操作。序列1、压力0的尾部示例：

|当前操作|wss_bytes|pagefault|
|---|---|---|
|5|[606982144, 140582912, 48627712, -1]|[165541, 10870, 3, -1]|
|6|[140582912, 48627712, -1, -1]|[10870, 3, -1, -1]|
|7|[48627712, -1, -1, -1]|[3, -1, -1, -1]|
|8|[-1, -1, -1, -1]|[-1, -1, -1, -1]|

上述查询均返回0。不补零，不重复末值；真实0次缺页合法。Python接口保持None表示空值，C接口使用有符号整数-1表示超出末尾。内部数据缺失仍报错，不用-1掩盖数据异常。

## 交接和编译

运行只需要get_wss_predictor.h、get_wss_predictor.c、gimp_lookup_data.h，放在同一源码目录，将.c加入调用方编译目标。不读Excel，不需要Python、初始化或动态分配。

```sh
cc -O2 -std=c99 -Wall -Wextra -Werror get_wss_predictor.c example.c -o example
./example
make test
```

头文件支持__KERNEL__类型；由内核同学加入Kbuild、处理必要的符号可见性。本机验证独立C程序及实际导出函数，没有编译完整定制内核，没有实现high/reclaim动作或实际阶段边界通知。

## 后续添加应用

在原项目c_api目录用真实应用数据导出，例如：

```text
python export_table.py --app firefox --data "Firefox数据.xlsx"
```

将生成firefox_lookup_data.h及对应manifest。--app仅决定命名，不核实数据确实属于该应用。非GIMP导出必须显式提供--data。

在get_wss_predictor.c现有数据头附近增加#include "firefox_lookup_data.h"，再在app_tables注册表加：

```c
{"firefox", firefox_lookup_rows, FIREFOX_LOOKUP_RECORD_COUNT, FIREFOX_LOOKUP_MAX_OPERATION}
```

重新编译和验证后即可使用get_wss_predictor("firefox", ...)。当前导出器沿用8操作、四压力档和现有列结构，不同数据结构须先适配。Python的apps.json与C注册表独立，不能只改JSON就认为C已经支持新应用。

## 数据与验证

总WSS直接读取原表WSS (bytes)，不把匿名和文件分项相加。缺页按原表阶段记录的总缺页次数原值回放，不自动把进程累计计数差分。采集窗口是否覆盖业务阶段仍需核实。

静态数据含实测和模拟，来源见data_manifest.json，不把模拟称为实测。当前源WSS均能无损表示为u64；非整数、负数、非有限或缺失值会标记无效，不强制取整。

Excel修改后需在原项目运行python export_table.py并重新编译。export_table.py和verify_c_api.py只供原项目更新与全表验证；接入运行不依赖它们，ZIP不含Python环境或Excel。

verify_c_api.py编译实际C代码，与独立重读的原Excel核对四步及全部8值。verification.json记录实际结果。tests覆盖内部早/晚缺口、早/晚指标缺失、零缺页、末尾不足四步、空/重叠数组以及失败不改任意输出。

这是已知轨迹Oracle，不是未知运行预测的100%准确率保证；操作编号不是预测刷新generation，也不能把查询未来四步当成实际已执行四步。
