import csv,json,collections,statistics
from pathlib import Path
B=Path(__file__).parent;O=B/'results';j=json.loads((O/'statistics.json').read_text());s=j['totals'];read=lambda n:list(csv.DictReader((O/n).open()));sessions=read('sessions.csv');events=read('merged_events.csv');samples=read('reentry_samples.csv');switches=read('switches.csv');repo=json.loads((B/'repository.json').read_text());root=B/'Reduced-A11y-CUA';missing=[x['rfilename'] for x in repo['siblings'] if not (root/x['rfilename']).is_file()]
files=[root/x['rfilename'] for x in repo['siblings'] if (root/x['rfilename']).exists()];size=sum(p.stat().st_size for p in files);assert not any(p.suffix.lower() in ('.mp4','.wav','.mp3','.webm') for p in files)
lines=[]
def p(x=''):lines.append(x)
def table(headers,rows):
 p('| '+' | '.join(headers)+' |');p('| '+' | '.join(['---']*len(headers))+' |')
 for row in rows:p('| '+' | '.join(str(v).replace('|','/') for v in row)+' |')
 p()
def n(v):return f'{v:,}'
def pct(v):return f'{v:.2%}'
p('# A11Y-CUA-FEASIBILITY-REPORT')
p();p('范围：完整 Reduced A11y-CUA 下载；仅 SU1–SU8 的 480 个独立任务 session 做语义应用识别、30-App 映射、事件合并、自环折叠和回访样本统计。未训练模型。');p()
p(f"**结论：480 个 session 中 {s['usable']} 个可构造交互序列；覆盖 {s['unique_target_apps']}/30 个目标应用，保留 {pct(s['switch_retention'])} 的源应用切换，得到 {n(s['reentry_events'])} 个已观察回访和 {n(s['censored_candidates'])} 个删失候选。** 这是按功能规则投影的短任务交互数据，不能据此认定完整 30-App 空间或长时间后台回访已获得充分覆盖。");p()
p('## 1. 数据版本与完整性');p()
p(f"来源：[Reduced A11y-CUA](https://huggingface.co/datasets/berkeley-hci/Reduced-A11y-CUA/tree/{j['revision']})；固定 revision `{j['revision']}`。官方标注约 3.86 GB，只去掉音视频，保留结构化文件。已下载实测 **{n(len(files))} 个文件、{n(size)} bytes（{size/1e9:.3f} GB / {size/2**30:.3f} GiB）**；剩余缺失文件 {len(missing)}，音视频文件 0。")
p('下载采用同一固定 revision 的 Git 批量包加 HTTPS 大文件补齐。普通文件与 Git blob 内容逐字节核对，51 个 LFS 文件按 Git 中记录的 SHA-256 与大小核对；最终下载清单另记录全部文件 SHA-256。下载期间发现 2 个非 SU 的截断响应，已隔离并重新下载；SU 校验未受影响。')
p('原始数据：`test/a11y_cua/Reduced-A11y-CUA/`。完整版本下载状态以上述缺失数为准；统计仅读取已完整下载的 SU；SU 文件逐个 JSON 解析，HTML 保留且纳入哈希校验，不把静态 DOM / accessibility tree 快照当作交互事件。');p()
p(f"SU 文件统计：`{json.dumps(j['file_counts'],ensure_ascii=False)}`。官方桌面事件参考数见[完整数据集说明](https://huggingface.co/datasets/berkeley-hci/A11y-CUA)：92,656；本次直接读取事件数组得到 {n(s['raw_desktop_events'])}，差值 {s['raw_desktop_events']-92656:+,}。metadata 声明的桌面事件总和为 {n(s['metadata_desktop_events'])}。");p()
p('## 2. 验收统计');p()
rows=[('SU users',s['users']),('sessions total',s['sessions']),('usable sessions',s['usable']),('raw desktop events',n(s['raw_desktop_events'])),('unique source apps',s['unique_source_apps']),('successfully mapped events',n(s['mapped_events'])),('mapping coverage',pct(s['mapping_coverage'])),('unique target Apps',f"{s['unique_target_apps']} / 30"),('raw App switches',n(s['raw_switches'])),('mapped switches',n(s['mapped_switches'])),('mapping-induced collapsed switches',n(s['collapsed_switches'])),('switch retention',pct(s['switch_retention'])),('reentry events',n(s['reentry_events']))]
labels=['C0 0–30s','C1 30–60s','C2 1–3min','C3 3–5min','C4 5–10min','C5 10–30min','C6 30–60min','C7 >60min']
rows.extend((label,n(s['bins'][f'C{i}'])) for i,label in enumerate(labels));rows.append(('censored candidates',n(s['censored_candidates'])));table(['项目','实测值'],rows)
p(f"主表 mapped events 的分母是合并、去重和时间边界检查后的 **{n(s['merged_events'])}** 条事件。单独按原始桌面数组口径，成功映射 **{n(s['mapped_desktop_events'])}/{n(s['raw_desktop_events'])} = {pct(s['desktop_mapping_coverage'])}**。原始 web interactions 共 {n(s['raw_web_events'])} 条；合并接纳量和排除原因见下表。");p()
table(['web 日志处理项','事件数'],[(k,n(v)) for k,v in j['web_merge_audit'].items()])
p('不可用 session：`SU5/16` 的 metadata 有起止时间，但桌面事件和 web 事件均为 0；其任务成功标记为 true，仍不能构造交互序列。');p()

desktop_edges=collections.Counter();last={}
for e in events:
 if e['origin']!='desktop':continue
 a=last.get(e['session']);last[e['session']]=e
 if a and a['source_app']!=e['source_app']:
  desktop_edges['raw']+=1
  status='unmapped' if not a['app_name'] or not e['app_name'] else 'collapsed' if a['app_name']==e['app_name'] else 'retained'
  desktop_edges[status]+=1
p(f"对照：完全不加入 web 事件时，桌面语义源切换 {n(desktop_edges['raw'])}，保留 {n(desktop_edges['retained'])}，映射合并 {n(desktop_edges['collapsed'])}，未映射端点 {n(desktop_edges['unmapped'])}。主表始终使用同一合并流作切换分子与分母。");p()
p('## 3. 定义与处理规则');p()
p('1. **session 可用**：metadata 起止时间有效，存在可排序的交互事件。任务成功与数据可用不同；不因 task.success=false 删除 session。480 个任务分别处理，绝不跨用户、跨任务拼接。')
p('2. **语义 source app**：根据事件 window.application、window.title、与窗口标题一致且已出现的 web URL 识别应用；ApplicationFrameHost 按窗口标题拆开，标题缺失时允许使用同宿主已出现的 accessibility tree 根标题。不根据鼠标所指图标、页面正文或任务 instruction 推断已切入目标应用。浏览器 New Tab、设置及未识别页面保留 ChromeBrowser/EdgeBrowser 来源，映射 Falkon，单列兜底数量。')
p('3. **30-App 映射**：app_id 严格使用项目预测词表 0–29；运行时 ID 1–30 不混用。Word/Excel/PowerPoint/Notepad/StickyNotes/OneNote 归到 LibreOffice；Windows Photos 归 Shotwell；在线视频归 VLC；地图归 Marble；搜索归 Konqueror；浏览器内 PDF 阅读归 Evince。任务视图 / Snap Assist 归 WindowsShell 未映射，不混入 Files；没有对应目标的天气应用保持未映射。此处是显式功能映射，不是进程、内存工作集或协议等价。浏览器内部的语义应用可以改变目标类别，但不会据此声称发生了 OS 进程切换。')
p('4. **事件合并**：桌面按 Unix 秒、web 按毫秒除 1000 归一化；先按 session 内时间排序。桌面完整重复项去重；web 仅在最近已观察桌面上下文为浏览器、且标题或语义应用匹配时接纳；blur 不表示新进入而排除；同来源同类型、±100ms 内已有桌面事件的 web 项排除。web 自身完整重复项去重。其余同应用事件经下一步折叠，不额外增加应用进入次数。')
p('5. **切换与自环**：raw switches 是合并流中相邻 semantic source 不同的边；两端有映射且 target 不同记 retained，相同记 mapping-induced collapsed，任一端未映射记 unmapped_endpoint。switch retention=retained/raw。折叠目标自环时保留 UNKNOWN 屏障，不把 A→UNKNOWN→A 拼成持续停留。')
p('6. **回访样本**：采用一次离开对应一个候选的 episode 口径。已知目标 A→已知目标 B 时，以 B 的首条交互时间作为 A 的离开时刻；A 下次出现构成回访，间隔=下次进入−离开。多个后台候选可并存，回访后再次离开可再建候选。未映射边界会终止当前观察块，并删失待返回候选；直接离开到 UNKNOWN 不建立不可确认候选。所有 pending 候选在 session 结束时右删失。')
p('7. **分箱**：C0 为 (0,30]，C1 为 (30,60]，C2 为 (60,180]，C3 为 (180,300]，C4 为 (300,600]，C5 为 (600,1800]，C6 为 (1800,3600]，C7 为 (3600,∞)。删失不是“永不返回”，也不自动归 C7。主表 C0–C7 仅统计真实观察到返回的 episode，总和等于 reentry events。')
p('8. **与训练样本的区别**：本轮不做每 30 秒的重复 query 展开、不划分训练验证集。这里的 censored candidates 是离开 episode 数，不是旧 SLAP 数据构建器中所有 query×candidate 行数。visited/background 只表示交互历史，不证明进程驻留、仍打开或内存冷热。');p()
p('## 4. 逐用户统计');p()
ur=[]
for user in range(1,9):
 rr=[x for x in sessions if x['user']==f'SU{user}'];sm=lambda k:sum(int(x[k]) for x in rr)
 ur.append((f'SU{user}',len(rr),sm('usable'),n(sm('raw_desktop_events')),n(sm('mapped_events')),n(sm('raw_switches')),n(sm('mapped_switches')),n(sm('collapsed_switches')),n(sm('reentry_events')),n(sm('censored_candidates'))))
table(['用户','sessions','usable','桌面事件','映射事件','源切换','保留切换','合并切换','回访','删失'],ur)
p('## 5. 全部语义来源与 30-App 映射');p()
table(['source_app','app_id','target app','功能类别','桌面事件','新增 web 事件'],[(x['source_app'],x['app_id'],x['app_name'] or '未映射',x['category'],n(x['desktop_events']),n(x['web_events'])) for x in j['mapping']])
p('识别依据计数：');p();table(['依据','事件数'],[(k,n(v)) for k,v in j['semantic_evidence_counts'].items()])
fallback=j['semantic_evidence_counts'].get('browser_fallback',0)
p(f"浏览器兜底事件 {n(fallback)}，占合并事件 {pct(fallback/s['merged_events'])}；它们计入功能映射覆盖率，但未识别具体站点应用。将这部分从成功识别数中扣除，覆盖率为 {pct((s['mapped_events']-fallback)/s['merged_events'])}，用于展示规则兜底的影响。");p()
vocab=json.loads((B.parents[1]/'lzx/tool/operation_predictor/data/vocab/lsapp_30/app_vocab_duration.json').read_text());target_counts=collections.Counter(e['app_name'] for e in events if e['app_name']);ret_counts=collections.Counter(x['app_name'] for x in samples if x['censored']=='0');cen_counts=collections.Counter(x['app_name'] for x in samples if x['censored']=='1')
table(['app_id','target app','映射事件','回访','删失'],[(i,a,n(target_counts[a]),n(ret_counts[a]),n(cen_counts[a])) for a,i in vocab.items() if i<30])
p('## 6. 切换损失与回访分布');p()
p(f"切换守恒：{n(s['raw_switches'])} = {n(s['mapped_switches'])} 保留 + {n(s['collapsed_switches'])} 映射合并 + {n(s['unmapped_switches'])} 未映射端点。删除未映射事件后再数切换会制造捷径，因此不采用该口径。");p()
p('映射合并最多的来源对：');p();pairs=collections.Counter((x['source_from'],x['source_to'],x['target_to']) for x in switches if x['status']=='collapsed');table(['source from','source to','共同 target','次数'],[(*k,n(v)) for k,v in pairs.most_common(20)])
p('删失原因：');p();table(['原因','候选数'],[(k,n(v)) for k,v in j['censor_reasons'].items()])
p(f"session 时长：最短 {s['session_duration_min']:.2f}s，中位 {s['session_duration_median']:.2f}s，最长 {s['session_duration_max']:.2f}s，总观察 {s['total_session_seconds']/3600:.2f} 小时。长时回访统计受每个 session 的观察长度限制，不能把不同任务之间的时间间隔填入 C5–C7。");p()
known=[float(x['reentry_s']) for x in samples if x['censored']=='0'];cens=[float(x['observed_s']) for x in samples if x['censored']=='1']
if known:p(f'已观察回访间隔：最短 {min(known):.4f}s，中位 {statistics.median(known):.2f}s，最长 {max(known):.2f}s。')
table(['阈值','session 时长达到阈值','删失候选至少观察到阈值'],[(f'{t}s',sum(float(x['duration_s'])>=t for x in sessions),sum(x>=t for x in cens)) for t in [30,60,180,300,600,1800,3600]])
p(f'极短返回：≤100ms 共 {sum(x<=.1 for x in known)} 个，≤1s 共 {sum(x<=1 for x in known)} 个；主表保留这些原始交互返回，不把它们自动解释为有意义的任务切换。');p()
p('## 7. 质量审计与可行性判断');p()
p('逐项审计发现 212 个 session 的 metadata 桌面事件总数与数组长度不同；另有 98 个按 session 去重的缺文件引用，全部属于声明 events=0 的空应用条目，没有声明正事件数却缺失的日志引用。实际文件扫描不依赖这些空条目的指针。');p()
p(f"metadata/文件问题记录数：{len(j['issues'])}。质量计数：`{json.dumps(j['quality_audit'],ensure_ascii=False)}`。逐条记录见 `results/issues.csv`；数量为 0 的检查也不代表连续焦点真值已获得验证。");p()
p('本数据可用于检查语义映射、短时任务中的应用返回和合并损失；不宜直接替代真实长期桌面使用轨迹。source app 是规则识别结果，尚无人审标注准确率；文件选择器、桌面 shell、录屏/日志程序等不强制归入无关目标，UNKNOWN 会降低连续观察量。功能映射将办公子应用压缩到同一目标，可能显著减少源切换。原始 OS 输入与 web 事件虽已去重和上下文过滤，仍不是持续记录的 foreground-change 真值。');p()
p('长尾是否可用应以本表 C4–C7 的实测支持数和曝光长度判断；0 表示本口径没有观察到相应返回，不表示应用永远不会长时返回。当前仅生成可复核统计和 episode CSV，不给出训练效果或部署可行性结论。');p()
p('## 8. 复现与产物');p()
p('```bash\ncd /home/lzx/Desktop/PARP\npython3 test/a11y_cua/download.py\npython3 test/a11y_cua/extract_git.py\npython3 test/a11y_cua/analyze.py\npython3 -m unittest discover -s test/a11y_cua -p test_semantics.py\npython3 test/a11y_cua/validate.py\npython3 test/a11y_cua/make_report.py\n```');p()
p('`test/a11y_cua/results/` 包含 `statistics.json`、480 行 `sessions.csv`、`source_app_mapping.csv`、带源文件/行索引的 `merged_events.csv`、`interaction_sequence.csv`、逐条 `switches.csv`、`reentry_samples.csv`、`issues.csv` 、`validation.json` 和 `su_file_hashes.csv`。下载清单及固定版本在 `repository.json` / `download_manifest.json`。')
p('独立输出校验：`validate.py` 从导出的 sequence 重新寻找每个候选的下一次返回，验证删失前无返回、离开归属正确、480 个用户任务键齐全及 5,367 个 SU 文件哈希未变；结果见 `validation.json`。')
p('校验：语义宿主拆分、搜索页面不误识别为目标站点、图标不当作前台、Google 服务区分、禁止未来 URL、边界分箱与删失、回访与删失构造、UNKNOWN 屏障、跨 session 隔离、任务视图识别、浏览器 PDF 识别共 11 项测试；分析中检查 480 session 可用性、切换守恒、分箱和回访总数守恒、正回访时间。')
(B.parents[1]/'A11Y-CUA-FEASIBILITY-REPORT.md').write_text('\n'.join(lines)+'\n');print('report generated')
