# A11Y-CUA-FEASIBILITY-REPORT

范围：完整 Reduced A11y-CUA 下载；仅 SU1–SU8 的 480 个独立任务 session 做语义应用识别、30-App 映射、事件合并、自环折叠和回访样本统计。未训练模型。

**结论：480 个 session 中 479 个可构造交互序列；覆盖 16/30 个目标应用，保留 39.08% 的源应用切换，得到 448 个已观察回访和 508 个删失候选。** 这是按功能规则投影的短任务交互数据，不能据此认定完整 30-App 空间或长时间后台回访已获得充分覆盖。

## 1. 数据版本与完整性

来源：[Reduced A11y-CUA](https://huggingface.co/datasets/berkeley-hci/Reduced-A11y-CUA/tree/26ee1103f74436cb7051543ba65d0c57902c0eb6)；固定 revision `26ee1103f74436cb7051543ba65d0c57902c0eb6`。官方标注约 3.86 GB，只去掉音视频，保留结构化文件。已下载实测 **13,881 个文件、3,864,666,811 bytes（3.865 GB / 3.599 GiB）**；剩余缺失文件 0，音视频文件 0。
下载采用同一固定 revision 的 Git 批量包加 HTTPS 大文件补齐。普通文件与 Git blob 内容逐字节核对，51 个 LFS 文件按 Git 中记录的 SHA-256 与大小核对；最终下载清单另记录全部文件 SHA-256。下载期间发现 2 个非 SU 的截断响应，已隔离并重新下载；SU 校验未受影响。
原始数据：`test/a11y_cua/Reduced-A11y-CUA/`。完整版本下载状态以上述缺失数为准；统计仅读取已完整下载的 SU；SU 文件逐个 JSON 解析，HTML 保留且纳入哈希校验，不把静态 DOM / accessibility tree 快照当作交互事件。

SU 文件统计：`{"desktop_log": 1101, "a11y_tree": 2899, "metadata": 480, "html": 341, "web_log": 546}`。官方桌面事件参考数见[完整数据集说明](https://huggingface.co/datasets/berkeley-hci/A11y-CUA)：92,656；本次直接读取事件数组得到 91,977，差值 -679。metadata 声明的桌面事件总和为 93,840。

## 2. 验收统计

| 项目 | 实测值 |
| --- | --- |
| SU users | 8 |
| sessions total | 480 |
| usable sessions | 479 |
| raw desktop events | 91,977 |
| unique source apps | 59 |
| successfully mapped events | 87,570 |
| mapping coverage | 90.26% |
| unique target Apps | 16 / 30 |
| raw App switches | 2,446 |
| mapped switches | 956 |
| mapping-induced collapsed switches | 94 |
| switch retention | 39.08% |
| reentry events | 448 |
| C0 0–30s | 393 |
| C1 30–60s | 36 |
| C2 1–3min | 17 |
| C3 3–5min | 2 |
| C4 5–10min | 0 |
| C5 10–30min | 0 |
| C6 30–60min | 0 |
| C7 >60min | 0 |
| censored candidates | 508 |

主表 mapped events 的分母是合并、去重和时间边界检查后的 **97,022** 条事件。单独按原始桌面数组口径，成功映射 **82,525/91,977 = 89.72%**。原始 web interactions 共 9,384 条；合并接纳量和排除原因见下表。

| web 日志处理项 | 事件数 |
| --- | --- |
| raw | 9,384 |
| paired_desktop_duplicate | 2,327 |
| accepted | 5,045 |
| blur_excluded | 1,059 |
| title_or_source_conflict | 229 |
| outside_session | 713 |
| nonbrowser_foreground | 11 |

不可用 session：`SU5/16` 的 metadata 有起止时间，但桌面事件和 web 事件均为 0；其任务成功标记为 true，仍不能构造交互序列。

对照：完全不加入 web 事件时，桌面语义源切换 2,442，保留 956，映射合并 90，未映射端点 1,396。主表始终使用同一合并流作切换分子与分母。

## 3. 定义与处理规则

1. **session 可用**：metadata 起止时间有效，存在可排序的交互事件。任务成功与数据可用不同；不因 task.success=false 删除 session。480 个任务分别处理，绝不跨用户、跨任务拼接。
2. **语义 source app**：根据事件 window.application、window.title、与窗口标题一致且已出现的 web URL 识别应用；ApplicationFrameHost 按窗口标题拆开，标题缺失时允许使用同宿主已出现的 accessibility tree 根标题。不根据鼠标所指图标、页面正文或任务 instruction 推断已切入目标应用。浏览器 New Tab、设置及未识别页面保留 ChromeBrowser/EdgeBrowser 来源，映射 Falkon，单列兜底数量。
3. **30-App 映射**：app_id 严格使用项目预测词表 0–29；运行时 ID 1–30 不混用。Word/Excel/PowerPoint/Notepad/StickyNotes/OneNote 归到 LibreOffice；Windows Photos 归 Shotwell；在线视频归 VLC；地图归 Marble；搜索归 Konqueror；浏览器内 PDF 阅读归 Evince。任务视图 / Snap Assist 归 WindowsShell 未映射，不混入 Files；没有对应目标的天气应用保持未映射。此处是显式功能映射，不是进程、内存工作集或协议等价。浏览器内部的语义应用可以改变目标类别，但不会据此声称发生了 OS 进程切换。
4. **事件合并**：桌面按 Unix 秒、web 按毫秒除 1000 归一化；先按 session 内时间排序。桌面完整重复项去重；web 仅在最近已观察桌面上下文为浏览器、且标题或语义应用匹配时接纳；blur 不表示新进入而排除；同来源同类型、±100ms 内已有桌面事件的 web 项排除。web 自身完整重复项去重。其余同应用事件经下一步折叠，不额外增加应用进入次数。
5. **切换与自环**：raw switches 是合并流中相邻 semantic source 不同的边；两端有映射且 target 不同记 retained，相同记 mapping-induced collapsed，任一端未映射记 unmapped_endpoint。switch retention=retained/raw。折叠目标自环时保留 UNKNOWN 屏障，不把 A→UNKNOWN→A 拼成持续停留。
6. **回访样本**：采用一次离开对应一个候选的 episode 口径。已知目标 A→已知目标 B 时，以 B 的首条交互时间作为 A 的离开时刻；A 下次出现构成回访，间隔=下次进入−离开。多个后台候选可并存，回访后再次离开可再建候选。未映射边界会终止当前观察块，并删失待返回候选；直接离开到 UNKNOWN 不建立不可确认候选。所有 pending 候选在 session 结束时右删失。
7. **分箱**：C0 为 (0,30]，C1 为 (30,60]，C2 为 (60,180]，C3 为 (180,300]，C4 为 (300,600]，C5 为 (600,1800]，C6 为 (1800,3600]，C7 为 (3600,∞)。删失不是“永不返回”，也不自动归 C7。主表 C0–C7 仅统计真实观察到返回的 episode，总和等于 reentry events。
8. **与训练样本的区别**：本轮不做每 30 秒的重复 query 展开、不划分训练验证集。这里的 censored candidates 是离开 episode 数，不是旧 SLAP 数据构建器中所有 query×candidate 行数。visited/background 只表示交互历史，不证明进程驻留、仍打开或内存冷热。

## 4. 逐用户统计

| 用户 | sessions | usable | 桌面事件 | 映射事件 | 源切换 | 保留切换 | 合并切换 | 回访 | 删失 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SU1 | 60 | 60 | 11,940 | 11,683 | 304 | 120 | 17 | 62 | 58 |
| SU2 | 60 | 60 | 6,362 | 6,292 | 258 | 106 | 5 | 46 | 60 |
| SU3 | 60 | 60 | 14,258 | 13,033 | 286 | 111 | 10 | 53 | 58 |
| SU4 | 60 | 60 | 11,529 | 10,938 | 273 | 112 | 13 | 57 | 55 |
| SU5 | 60 | 59 | 13,020 | 12,824 | 277 | 115 | 13 | 48 | 67 |
| SU6 | 60 | 60 | 13,276 | 12,417 | 229 | 85 | 11 | 29 | 56 |
| SU7 | 60 | 60 | 12,047 | 11,771 | 485 | 204 | 12 | 109 | 95 |
| SU8 | 60 | 60 | 9,545 | 8,612 | 334 | 103 | 13 | 44 | 59 |

## 5. 全部语义来源与 30-App 映射

| source_app | app_id | target app | 功能类别 | 桌面事件 | 新增 web 事件 |
| --- | --- | --- | --- | --- | --- |
| AppleWebsite | 15 | Falkon | 产品网页 | 2,862 | 756 |
| BingSearch | 16 | Konqueror | 搜索 | 84 | 6 |
| BrowserPDFReader | 6 | Evince | PDF阅读 | 96 | 0 |
| Calculator | 8 | Calculator | 计算器 | 2,267 | 0 |
| ChromeBrowser | 15 | Falkon | 浏览器界面/未识别网页 | 5,690 | 0 |
| DuckDuckGo | 16 | Konqueror | 搜索 | 477 | 230 |
| EdgeBrowser | 15 | Falkon | 浏览器界面/未识别网页 | 121 | 0 |
| Excel | 1 | LibreOffice | 表格编辑 | 8,498 | 0 |
| Expedia | 15 | Falkon | 旅行网页 | 1,800 | 443 |
| Facebook | 0 | Firefox | 社交网页 | 279 | 2 |
| FileExplorer | 7 | Files | 文件管理 | 14,544 | 0 |
| FilePicker |  | 未映射 | 未映射 | 560 | 0 |
| FontViewer |  | 未映射 | 未映射 | 178 | 0 |
| France24 | 6 | Evince | 资讯阅读 | 254 | 28 |
| GoogleAccount | 15 | Falkon | 网页账号 | 22 | 1 |
| GoogleForms | 15 | Falkon | 网页表单 | 865 | 310 |
| GoogleMaps | 27 | Marble | 地图 | 1,396 | 479 |
| GooglePlay | 22 | GNOMESoftware | 应用商店 | 32 | 0 |
| GoogleSearch | 16 | Konqueror | 搜索 | 1,429 | 331 |
| Instagram | 0 | Firefox | 社交网页 | 8 | 0 |
| Installer |  | 未映射 | 未映射 | 16 | 0 |
| MSNWeather |  | 未映射 | 未映射 | 60 | 0 |
| MediaPlayerSetup |  | 未映射 | 未映射 | 21 | 0 |
| Notepad | 1 | LibreOffice | 文本编辑 | 1,196 | 0 |
| OneNote | 1 | LibreOffice | 笔记 | 2 | 0 |
| OpenWithDialog |  | 未映射 | 未映射 | 56 | 0 |
| Paint | 3 | GIMP | 图像编辑 | 1,365 | 0 |
| PowerPoint | 1 | LibreOffice | 演示编辑 | 8,160 | 0 |
| Recorder |  | 未映射 | 未映射 | 200 | 0 |
| Slack | 17 | Pidgin | 即时通信 | 73 | 0 |
| SnippingTool |  | 未映射 | 未映射 | 2,608 | 0 |
| StickyNotes | 1 | LibreOffice | 笔记 | 990 | 0 |
| Target | 0 | Firefox | 购物网页 | 2,100 | 680 |
| TaskManager | 13 | SystemMonitor | 资源监控 | 231 | 0 |
| UnresolvedApplicationFrameHost |  | 未映射 | 未映射 | 24 | 0 |
| VLC | 2 | VLC | 媒体播放 | 92 | 0 |
| VSCode |  | 未映射 | 未映射 | 48 | 0 |
| Walmart | 0 | Firefox | 购物网页 | 960 | 272 |
| Web:azlyrics.com | 15 | Falkon | 其他网页 | 108 | 12 |
| Web:genius.com | 15 | Falkon | 其他网页 | 46 | 31 |
| Web:grammarly.com | 15 | Falkon | 其他网页 | 7 | 1 |
| Web:norcal.hondadealers.com | 15 | Falkon | 其他网页 | 17 | 1 |
| Web:oklahoman.com | 15 | Falkon | 其他网页 | 980 | 65 |
| Web:pdfcrowd.com | 15 | Falkon | 其他网页 | 50 | 7 |
| Web:studycat.com | 15 | Falkon | 其他网页 | 139 | 4 |
| Web:subaru.com | 15 | Falkon | 其他网页 | 53 | 3 |
| Web:tinyurl.com | 15 | Falkon | 其他网页 | 16 | 1 |
| Web:toyota.com | 15 | Falkon | 其他网页 | 14 | 1 |
| WebCalculator | 8 | Calculator | 网页计算器 | 31 | 0 |
| Wikipedia | 6 | Evince | 参考阅读 | 1,071 | 83 |
| WindowsClock | 25 | GNOMEClocks | 时间工具 | 794 | 0 |
| WindowsMediaPlayer | 2 | VLC | 媒体播放 | 4,382 | 0 |
| WindowsPhotos | 12 | Shotwell | 照片管理 | 4,693 | 0 |
| WindowsSettings | 29 | GNOMEControlCenter | 系统设置 | 1,966 | 0 |
| WindowsShell |  | 未映射 | 未映射 | 3,714 | 0 |
| WindowsWeather |  | 未映射 | 未映射 | 1,927 | 0 |
| WindowsWidgets |  | 未映射 | 未映射 | 40 | 0 |
| Word | 1 | LibreOffice | 文档编辑 | 8,258 | 0 |
| YouTube | 2 | VLC | 在线视频 | 4,007 | 1,298 |

识别依据计数：

| 依据 | 事件数 |
| --- | --- |
| process | 36,387 |
| shell_window | 3,714 |
| browser_fallback | 5,811 |
| window_title | 4,248 |
| matched_window_title_url | 14,919 |
| web_url_with_desktop_context | 5,045 |
| window_title_pdf | 96 |
| host_window_title | 12,066 |
| explorer_content_window | 14,544 |
| shell_title | 168 |
| unresolved_host | 24 |

浏览器兜底事件 5,811，占合并事件 5.99%；它们计入功能映射覆盖率，但未识别具体站点应用。将这部分从成功识别数中扣除，覆盖率为 84.27%，用于展示规则兜底的影响。

| app_id | target app | 映射事件 | 回访 | 删失 |
| --- | --- | --- | --- | --- |
| 0 | Firefox | 4,301 | 20 | 3 |
| 1 | LibreOffice | 27,104 | 96 | 59 |
| 2 | VLC | 9,779 | 13 | 18 |
| 3 | GIMP | 1,365 | 1 | 2 |
| 4 | Audacity | 0 | 0 | 0 |
| 5 | Thunderbird | 0 | 0 | 0 |
| 6 | Evince | 1,532 | 22 | 12 |
| 7 | Files | 14,544 | 152 | 207 |
| 8 | Calculator | 2,298 | 1 | 0 |
| 9 | Calendar | 0 | 0 | 0 |
| 10 | Rhythmbox | 0 | 0 | 0 |
| 11 | ImageViewer | 0 | 0 | 0 |
| 12 | Shotwell | 4,693 | 8 | 12 |
| 13 | SystemMonitor | 231 | 0 | 1 |
| 14 | Solitaire | 0 | 0 | 0 |
| 15 | Falkon | 14,426 | 98 | 116 |
| 16 | Konqueror | 2,557 | 35 | 62 |
| 17 | Pidgin | 73 | 0 | 5 |
| 18 | Gajim | 0 | 0 | 0 |
| 19 | Dino | 0 | 0 | 0 |
| 20 | PsiPlus | 0 | 0 | 0 |
| 21 | Kaidan | 0 | 0 | 0 |
| 22 | GNOMESoftware | 32 | 0 | 1 |
| 23 | Evolution | 0 | 0 | 0 |
| 24 | ClawsMail | 0 | 0 | 0 |
| 25 | GNOMEClocks | 794 | 0 | 0 |
| 26 | GNOMEContacts | 0 | 0 | 0 |
| 27 | Marble | 1,875 | 1 | 6 |
| 28 | GNOMEMines | 0 | 0 | 0 |
| 29 | GNOMEControlCenter | 1,966 | 1 | 4 |

## 6. 切换损失与回访分布

切换守恒：2,446 = 956 保留 + 94 映射合并 + 1,396 未映射端点。删除未映射事件后再数切换会制造捷径，因此不采用该口径。

映射合并最多的来源对：

| source from | source to | 共同 target | 次数 |
| --- | --- | --- | --- |
| ChromeBrowser | Expedia | Falkon | 14 |
| ChromeBrowser | GoogleForms | Falkon | 8 |
| Expedia | ChromeBrowser | Falkon | 8 |
| ChromeBrowser | Web:oklahoman.com | Falkon | 8 |
| ChromeBrowser | AppleWebsite | Falkon | 6 |
| Web:oklahoman.com | ChromeBrowser | Falkon | 6 |
| PowerPoint | Excel | LibreOffice | 5 |
| Word | PowerPoint | LibreOffice | 4 |
| ChromeBrowser | Web:azlyrics.com | Falkon | 4 |
| Excel | PowerPoint | LibreOffice | 3 |
| Word | StickyNotes | LibreOffice | 3 |
| ChromeBrowser | Web:subaru.com | Falkon | 3 |
| StickyNotes | Word | LibreOffice | 2 |
| ChromeBrowser | Web:toyota.com | Falkon | 2 |
| Web:toyota.com | Web:subaru.com | Falkon | 2 |
| Web:azlyrics.com | ChromeBrowser | Falkon | 2 |
| Web:studycat.com | ChromeBrowser | Falkon | 1 |
| ChromeBrowser | Web:studycat.com | Falkon | 1 |
| Web:subaru.com | ChromeBrowser | Falkon | 1 |
| ChromeBrowser | Web:tinyurl.com | Falkon | 1 |

删失原因：

| 原因 | 候选数 |
| --- | --- |
| unknown_boundary | 335 |
| session_end | 173 |

session 时长：最短 13.91s，中位 65.62s，最长 658.34s，总观察 12.37 小时。长时回访统计受每个 session 的观察长度限制，不能把不同任务之间的时间间隔填入 C5–C7。

已观察回访间隔：最短 0.0101s，中位 5.39s，最长 259.45s。
| 阈值 | session 时长达到阈值 | 删失候选至少观察到阈值 |
| --- | --- | --- |
| 30s | 435 | 180 |
| 60s | 270 | 82 |
| 180s | 58 | 8 |
| 300s | 9 | 1 |
| 600s | 1 | 0 |
| 1800s | 0 | 0 |
| 3600s | 0 | 0 |

极短返回：≤100ms 共 19 个，≤1s 共 91 个；主表保留这些原始交互返回，不把它们自动解释为有意义的任务切换。

## 7. 质量审计与可行性判断

逐项审计发现 212 个 session 的 metadata 桌面事件总数与数组长度不同；另有 98 个按 session 去重的缺文件引用，全部属于声明 events=0 的空应用条目，没有声明正事件数却缺失的日志引用。实际文件扫描不依赖这些空条目的指针。

metadata/文件问题记录数：311。质量计数：`{"invalid_desktop_timestamp": 0, "desktop_outside_metadata_bounds": 0, "duplicate_desktop_events": 0, "departures_into_unknown_not_candidates": 546}`。逐条记录见 `results/issues.csv`；数量为 0 的检查也不代表连续焦点真值已获得验证。

本数据可用于检查语义映射、短时任务中的应用返回和合并损失；不宜直接替代真实长期桌面使用轨迹。source app 是规则识别结果，尚无人审标注准确率；文件选择器、桌面 shell、录屏/日志程序等不强制归入无关目标，UNKNOWN 会降低连续观察量。功能映射将办公子应用压缩到同一目标，可能显著减少源切换。原始 OS 输入与 web 事件虽已去重和上下文过滤，仍不是持续记录的 foreground-change 真值。

长尾是否可用应以本表 C4–C7 的实测支持数和曝光长度判断；0 表示本口径没有观察到相应返回，不表示应用永远不会长时返回。当前仅生成可复核统计和 episode CSV，不给出训练效果或部署可行性结论。

## 8. 复现与产物

```bash
cd /home/lzx/Desktop/PARP
python3 test/a11y_cua/download.py
python3 test/a11y_cua/extract_git.py
python3 test/a11y_cua/analyze.py
python3 -m unittest discover -s test/a11y_cua -p test_semantics.py
python3 test/a11y_cua/validate.py
python3 test/a11y_cua/make_report.py
```

`test/a11y_cua/results/` 包含 `statistics.json`、480 行 `sessions.csv`、`source_app_mapping.csv`、带源文件/行索引的 `merged_events.csv`、`interaction_sequence.csv`、逐条 `switches.csv`、`reentry_samples.csv`、`issues.csv` 、`validation.json` 和 `su_file_hashes.csv`。下载清单及固定版本在 `repository.json` / `download_manifest.json`。
独立输出校验：`validate.py` 从导出的 sequence 重新寻找每个候选的下一次返回，验证删失前无返回、离开归属正确、480 个用户任务键齐全及 5,367 个 SU 文件哈希未变；结果见 `validation.json`。
校验：语义宿主拆分、搜索页面不误识别为目标站点、图标不当作前台、Google 服务区分、禁止未来 URL、边界分箱与删失、回访与删失构造、UNKNOWN 屏障、跨 session 隔离、任务视图识别、浏览器 PDF 识别共 11 项测试；分析中检查 480 session 可用性、切换守恒、分箱和回访总数守恒、正回访时间。
