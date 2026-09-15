# Excel 自动化映射（第一阶段）

源文件 SHA-256：`f6f26ee836d98ca0b84f494418daf0d0bac27c182b82b0138709a060362b47ca`

共 2615 条、12 组。计划统计：{'skip': 291, 'substitute': 2324}。这些不是执行成功数。

| 原应用 | 记录数 | 目标应用 | 可执行计划 | 明确跳过 |
|---|---:|---|---:|---:|
| 腾讯会议 | 44 | — | 0 | 44 |
| 浏览器 | 704 | FIREFOX, LIBREOFFICE | 688 | 16 |
| 海泰浏览器 | 512 | FALKON | 512 | 0 |
| 文件管理器 | 279 | FILES, FILE_ROLLER, IMAGE_VIEWER, WPS | 235 | 44 |
| WPS | 450 | WPS | 435 | 15 |
| 应用市场 | 54 | SOFTWARE | 1 | 53 |
| 应用中心 | 68 | CONTROL_CENTER, FILES, FIREFOX, GIMP, KAIDAN, LIBREOFFICE, MOUSEPAD, SHOTCUT, WPS | 43 | 25 |
| 企业微信 | 2 | KAIDAN | 2 | 0 |
| 桌面 | 58 | — | 46 | 12 |
| 飞书 | 14 | — | 0 | 14 |
| 小艺 | 20 | — | 0 | 20 |
| 备忘录 | 13 | MOUSEPAD | 9 | 4 |
| 腾讯文档 | 82 | LIBREOFFICE | 79 | 3 |
| QQ音乐 | 42 | RHYTHMBOX | 30 | 12 |
| 好压 | 13 | FILES, FILE_ROLLER | 13 | 0 |
| 图库 | 152 | FILES, IMAGE_VIEWER, VLC | 136 | 16 |
| 剪映 | 12 | SHOTCUT | 12 | 0 |
| 虚拟机 | 9 | — | 0 | 9 |
| 抖音 | 37 | FIREFOX | 37 | 0 |
| 悟空图像 | 50 | GIMP | 46 | 4 |

FIREFOX 是已有运行时键，实际启动 Epiphany。图库按动作分别使用 Files、Image Viewer、VLC；抖音使用浏览器本地视频页。
腾讯文档使用 LibreOffice 本地文件，不验证在线协作。飞书记录全部为会议，因此不映射为聊天动作。
应用中心/文件管理器中的 WPS、CAD 等按操作描述修正归属，同时保留原始 app_name。

完整逐行依据见 mapping.csv；原始来源字段全部保存在 plan.json。
