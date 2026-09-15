# LSApp 30应用映射与自动化设计

状态：映射及覆盖审计完成；新增15应用基础GUI验收通过，完整30应用预测和场景集成仍待验证。旧15应用配置、checkpoint、常驻服务保持原样。
保留原15个真实应用词表ID，新应用追加15—29；PAD/UNKNOWN移到30/31。runtime_app_id沿用1—15并追加16—30。
当前myfs桥支持最多32个真实应用条目；30个真实应用可容纳，两个特殊词表项不下发。新词表必须配套重训，不能直接复用旧15应用checkpoint。

| 指标 | 15应用旧映射 | 30应用新映射 |
|---|---:|---:|
| 原始事件覆盖率 | 68.09% | 91.32% |
| Opened/Interaction事件覆盖率 | 66.51% | 91.60% |
| 原始相邻应用切换保留率 | 22.18% | 82.57% |

切换审计以同用户、间隔≤3600秒的原始Opened/Interaction相邻不同应用为分母，共313642次；两端都映射且目标不同才算保留。未映射端点与同目标合并均算未保留。它不是模型召回率、不是焦点事件完整性证明，也不是内存工作集等价证明。

## 新增15个应用

| runtime ID | 应用 | LSApp来源 | 自动化动作 |
|---|---|---|---|
| 16 | Falkon | Google Chrome, Samsung Internet Browser, Brave Browser | 加载本地网页、滚动、查找、切换标签 |
| 17 | Konqueror | Google, Microsoft Bing Search | 在本地搜索页输入查询、打开结果、返回 |
| 18 | Pidgin | Facebook Messenger, Messenger Lite, Discord | 本地XMPP会话切换、发送测试消息、滚动历史 |
| 19 | Gajim | WhatsApp Messenger, imo | 本地XMPP消息输入、查看历史、传输本地图片 |
| 20 | Dino | Telegram, Telegram X | 本地XMPP切换会话、收发消息、浏览历史 |
| 21 | PsiPlus | Messages, Verizon Messages, Messaging, TextNow, Text One | 本地XMPP文本会话；不模拟SMS网络 |
| 22 | Kaidan | WeChat, Hangouts, Kik | 本地XMPP会话、文本消息和图片浏览 |
| 23 | GNOMESoftware | Google Play Store | 浏览已安装应用和预缓存详情；不安装卸载软件 |
| 24 | Evolution | Samsung Email, Microsoft Outlook | 导入本地邮件、切换文件夹、阅读和搜索 |
| 25 | ClawsMail | Yahoo Mail, AOL | 导入mbox、阅读邮件、搜索和切换文件夹 |
| 26 | GNOMEClocks | Clock | 切换时钟页、启动停止秒表、设置后取消计时器 |
| 27 | GNOMEContacts | Contacts | 导入合成联系人、搜索、打开和编辑测试条目 |
| 28 | Marble | Maps | 加载预置地图、平移缩放、查看已准备的地点 |
| 29 | GNOMEMines | Minesweeper Classic (Mines) | 新建固定难度游戏、点击方格、重新开始 |
| 30 | GNOMEControlCenter | Settings | 导航显示/声音/系统信息页；不修改系统设置 |

## 保留的15个应用及调整后的来源

| 应用 | LSApp来源 |
|---|---|
| Firefox | Facebook, Instagram, Twitter, Reddit, Pinterest, Quora, Amazon Shopping, OfferUp, eBay, Walmart, PayPal Mobile Cash, Robinhood, Samsung Pay |
| LibreOffice | Samsung Notes |
| VLC | Swagbucks Watch (TV), YouTube, Netflix, Hulu, Movie Play Box, EntertaiNow |
| GIMP | Faceu, Pixlr |
| Audacity | Podcast Addict |
| Thunderbird | Gmail |
| Evince | Flipboard Briefing |
| Files | Google Drive |
| Calculator | Calculator, Calorie Counter, DigiHUD Pro Speedometer |
| Calendar | Calendar |
| Rhythmbox | Spotify Music, Pandora Music, Google Play Music |
| ImageViewer | Google Photos, Flickr |
| Shotwell | Samsung Gallery, Camera |
| SystemMonitor | MAX Cleaner, Clean Master |
| Solitaire | Army Men Strike, Words With Friends 2, Baseball Boy! |

## 自动化实现约束

- 聊天类：使用仅本机可达的私有Prosody XMPP服务器和合成测试账号，独立profile。禁止依赖真实Messenger/WhatsApp/Telegram/微信账号；这些客户端只替代会话、文本、图片等交互，不替代原网络协议或完整工作集。
- 邮件类：使用合成mbox/邮件文件与本地联系人数据，不连接真实邮箱。
- 浏览器类：加载同一实验内固定的本地页面资产。现有FIREFOX条目实际执行Epiphany，报告中必须披露；本方案没有再增加一个Epiphany条目重复计数。
- 地图：先准备本地地图资产，不以公网地图可用性决定场景结果。应用商店：仅查看已安装/缓存条目，不进行系统安装操作。
- 每个应用必须通过启动、独立内容窗口、焦点切换、至少一种应用内操作、子进程归属、退出清理验收。仅存在软件包或能打开主窗口不算自动化完成。
- XMPP服务器和测试回复器单独计量，Native/PARP共用相同外部基础设施条件，不混入某个聊天应用的cgroup。
- GNOME/Qt应用共享服务和辅助进程要核对归属，不能对多应用重复计量。单实例应用应使用私有会话总线和profile，不能影响用户现有窗口。
- 不要求30个应用每轮同时运行。工作集与前台序列必须来自映射后的历史，不随机指定冷热。

## 数据处理约束

- 先按原始应用身份维护事件状态，再投影为目标应用集合，防止同目标两个源应用之一Closed时误删另一源应用。
- Closed不直接当作进程退出真值；事件推导候选、会话已访问候选、真实PC运行集合分别报告。
- 未映射应用保留显式未知边界，或将跨越该边界的场景判不可回放；不能删掉中间应用再把两端同应用拼成持续停留。
- 30目标映射仍是多对一，仍存在信息损失。功能关联不等于应用级内存特性等价；不能将映射覆盖率提升解释为预测效果提升。
- 30应用模型的训练、验证、测试和checkpoint独立保存，以验证集选择保护策略，并报告多候选场景的端到端返回召回。

## 明确未映射的源应用

Phone, Slidejoy, Lucktastic, Android In Call UI, Swagbucks, SurveyCow, S’more, Snapchat, Reward Stash, The PCH App, MetroZone, Receipt Hog, Ibotta, MUIQ Survey App, Badoo

这些应用没有在本方案中强行分配到无关程序。未映射计数见mapping-audit.json。

## 实施顺序

先对新增应用做GUI可操作性与归属验收；验收不通过则调整替代程序和映射版本。然后按上述边界和候选规则生成新数据、重训和离线评估。最后才用固定动作计划进行Native/PARP性能对比。

## 依据

- [Pidgin XMPP](https://www.pidgin.im/help/protocols/xmpp/)、[Gajim](https://gajim.org/)、[Dino](https://dino.im/)、[Prosody测试账号](https://prosody.im/doc/creating_accounts)。
- [Falkon](https://www.falkon.org/about/)、[Konqueror](https://apps.kde.org/konqueror/)、[Claws Mail本地邮件导入](https://claws-mail.org/features.php)。
- 当前机器软件包候选版本见package-availability.txt；功能可行性不等于已在本机完成GUI验证。

GUI验收证据、启动/回放入口和限制见 [README-lsapp-30-gui.md](README-lsapp-30-gui.md)。
