"""Statistics-only, deterministic semantic mapping; no training or deployment."""
import bisect,collections,csv,hashlib,json,math,re,statistics
from pathlib import Path
from urllib.parse import urlparse
BASE=Path(__file__).parent; ROOT=BASE/'Reduced-A11y-CUA'; OUT=BASE/'results';OUT.mkdir(exist_ok=True)
VOCAB=json.loads((BASE.parents[1]/'lzx/tool/operation_predictor/data/vocab/lsapp_30/app_vocab_duration.json').read_text())
MAP={
'FileExplorer':('Files','文件管理'),'Word':('LibreOffice','文档编辑'),'Excel':('LibreOffice','表格编辑'),'PowerPoint':('LibreOffice','演示编辑'),'Notepad':('LibreOffice','文本编辑'),'StickyNotes':('LibreOffice','笔记'),'OneNote':('LibreOffice','笔记'),
'Calculator':('Calculator','计算器'),'WindowsSettings':('GNOMEControlCenter','系统设置'),'WindowsClock':('GNOMEClocks','时间工具'),'WindowsPhotos':('Shotwell','照片管理'),'Paint':('GIMP','图像编辑'),'WindowsMediaPlayer':('VLC','媒体播放'),'VLC':('VLC','媒体播放'),'TaskManager':('SystemMonitor','资源监控'),'Slack':('Pidgin','即时通信'),
'BrowserPDFReader':('Evince','PDF阅读'),'WebCalculator':('Calculator','网页计算器'),'Instagram':('Firefox','社交网页'),'ChromeBrowser':('Falkon','浏览器界面/未识别网页'),'EdgeBrowser':('Falkon','浏览器界面/未识别网页'),'GoogleSearch':('Konqueror','搜索'),'BingSearch':('Konqueror','搜索'),'DuckDuckGo':('Konqueror','搜索'),'YouTube':('VLC','在线视频'),'GoogleMaps':('Marble','地图'),'GoogleForms':('Falkon','网页表单'),'GoogleDocs':('LibreOffice','在线文档'),'GoogleSheets':('LibreOffice','在线表格'),'GoogleSlides':('LibreOffice','在线演示'),'GooglePlay':('GNOMESoftware','应用商店'),'Facebook':('Firefox','社交网页'),'Target':('Firefox','购物网页'),'Walmart':('Firefox','购物网页'),'Expedia':('Falkon','旅行网页'),'AppleWebsite':('Falkon','产品网页'),'Wikipedia':('Evince','参考阅读'),'France24':('Evince','资讯阅读'),'GoogleAccount':('Falkon','网页账号'),
}
NATIVE={'winword.exe':'Word','excel.exe':'Excel','powerpnt.exe':'PowerPoint','notepad.exe':'Notepad','onenote.exe':'OneNote','photos.exe':'WindowsPhotos','microsoft.photos.exe':'WindowsPhotos','mspaint.exe':'Paint','vlc.exe':'VLC','wmplayer.exe':'WindowsMediaPlayer','taskmgr.exe':'TaskManager','slack.exe':'Slack','snippingtool.exe':'SnippingTool','fontview.exe':'FontViewer','pickerhost.exe':'FilePicker','openwith.exe':'OpenWithDialog','code.exe':'VSCode','python3.13.exe':'Recorder','widgets.exe':'WindowsWidgets','update.exe':'Installer','setup_wm.exe':'MediaPlayerSetup'}
TITLE_RULES=[('Google Maps','GoogleMaps'),('Google Search','GoogleSearch'),(' - Bing','BingSearch'),('DuckDuckGo','DuckDuckGo'),('YouTube','YouTube'),('Google Play','GooglePlay'),('Google Forms','GoogleForms'),('Anonymous Feedback form on School Facility','GoogleForms'),('Google Docs','GoogleDocs'),('Google Sheets','GoogleSheets'),('Google Slides','GoogleSlides'),('Wikipedia','Wikipedia'),('Facebook','Facebook'),('Instagram','Instagram'),('Walmart','Walmart'),('Target','Target'),('Expedia','Expedia'),('AUS to NYC flights','Expedia'),('NYC to AUS flights','Expedia'),('France 24','France24'),('Apple','AppleWebsite')]
def web_source(url,title=''):
 h=urlparse(url).hostname or ''; path=urlparse(url).path;h=h.lower().removeprefix('www.')
 if h=='docs.google.com':return 'GoogleForms' if '/forms' in path else 'GoogleSheets' if '/spreadsheets' in path else 'GoogleSlides' if '/presentation' in path else 'GoogleDocs'
 if h=='maps.google.com' or (h=='google.com' and path.startswith('/maps')):return 'GoogleMaps'
 for domain,source in [('youtube.com','YouTube'),('youtu.be','YouTube'),('google.com','GoogleSearch'),('bing.com','BingSearch'),('duckduckgo.com','DuckDuckGo'),('play.google.com','GooglePlay'),('facebook.com','Facebook'),('target.com','Target'),('walmart.com','Walmart'),('expedia.com','Expedia'),('apple.com','AppleWebsite'),('wikipedia.org','Wikipedia'),('france24.com','France24'),('accounts.google.com','GoogleAccount')]:
  if h==domain or (domain!='google.com' and h.endswith('.'+domain)):return source
 if h:
  source='Web:'+h; MAP.setdefault(source,('Falkon','其他网页'));return source
 return None

def normalized(title):return re.sub(r'\s+-\s+(Google Chrome|Microsoft Edge).*$', '',title).strip().casefold()
def semantic(e,app,tabs,trees):
 w=e.get('window') or {}; app=(w.get('application') or app).lower();title=w.get('title') or '';t=e['timestamp'];evidence='process'
 if app in ('chrome.exe','msedge.exe'):
  if re.search(r'\.pdf(?:\s+-|$)',title,re.I):return 'BrowserPDFReader','window_title_pdf'
  if 'calculator.com' in title.lower():return 'WebCalculator','window_title'
  if 'MSN Weather' in title:return 'MSNWeather','window_title'
  # Window title and URL evidence only; never infer active app from clicked text or task instruction.
  matches=[x for x in tabs if x['created']<=t and normalized(x['title'])==normalized(title) and normalized(title)]
  if matches:return web_source(max(matches,key=lambda x:x['created'])['url'],title) or 'ChromeBrowser','matched_window_title_url'
  for token,source in TITLE_RULES:
   if token.casefold() in title.casefold():return source,'window_title'
  return ('ChromeBrowser' if app=='chrome.exe' else 'EdgeBrowser'),'browser_fallback'
 if app=='applicationframehost.exe':
  if not title:
   prior=[x for x in trees if x[0]<=t and x[1].lower()==app]
   if prior:title=max(prior)[2];evidence='a11y_root'
  for token,source in [('Calculator','Calculator'),('Settings','WindowsSettings'),('Media Player','WindowsMediaPlayer'),('Films & TV','WindowsMediaPlayer'),('Movies & TV','WindowsMediaPlayer'),('Sticky Notes','StickyNotes'),('Clock','WindowsClock'),('Weather','WindowsWeather'),('Photos','WindowsPhotos')]:
   if token.casefold() in title.casefold():return source,evidence if evidence=='a11y_root' else 'host_window_title'
  return 'UnresolvedApplicationFrameHost','unresolved_host'
 if app=='explorer.exe':
  if not title or title in ('Program Manager','Task Switching','Task View','Snap Assist') or 'tray' in title.lower():return 'WindowsShell','shell_window'
  return 'FileExplorer','explorer_content_window'
 if app in ('shellhost.exe','shellexperiencehost.exe'):
  return ('WindowsSettings' if 'settings' in title.lower() else 'WindowsShell'),'shell_title'
 if app in ('systemsettings.exe',):return 'WindowsSettings','process'
 return NATIVE.get(app,'Process:'+app),'process'

def etype(e):
 x=e.get('event',e.get('type','unknown'));return x.get('type','unknown') if isinstance(x,dict) else x

def csvout(name,rows,fields=None):
 if not rows and not fields:return
 with (OUT/name).open('w') as f:
  w=csv.DictWriter(f,fieldnames=fields or list(rows[0]));w.writeheader();w.writerows(rows)

def run():
 repo=json.loads((BASE/'repository.json').read_text());expected=[x['rfilename'] for x in repo['siblings'] if x['rfilename'].startswith('SU/')]
 missing=[n for n in expected if not (ROOT/n).exists()]
 if missing:raise RuntimeError(f'SU download incomplete: {len(missing)} files missing')
 issues=[];session_rows=[];sources=collections.defaultdict(collections.Counter);all_events=[];all_segments=[];all_samples=[];all_switches=[];web_audit=collections.Counter();filecounts=collections.Counter();audit=collections.Counter({'invalid_desktop_timestamp':0,'desktop_outside_metadata_bounds':0,'duplicate_desktop_events':0,'departures_into_unknown_not_candidates':0});durations=[];full_sources=collections.Counter();hashes=[]
 for user in range(1,9):
  for task in range(1,61):
   sid=f'SU{user}/{task}';folder=ROOT/'SU'/sid;mp=folder/f'metadata_{task}.json';m=json.loads(mp.read_text());start=m['session']['started_at'];end=m['session']['ended_at'];durations.append(end-start);desk=[];tabs=[];trees=[];raw=0;webraw=0;meta_count=sum(a.get('events',0) for a in m['session']['applications']);meta_refs={Path(a['log_file']).name for a in m['session']['applications'] if a.get('log_file')}
   for p in sorted(folder.rglob('*')):
    if not p.is_file():continue
    data=p.read_bytes();hashes.append((str(p.relative_to(ROOT)),len(data),hashlib.sha256(data).hexdigest()))
    if p.suffix=='.html':filecounts['html']+=1;continue
    if p.suffix!='.json':filecounts['other']+=1;continue
    try:d=json.loads(data)
    except Exception as ex:issues.append({'session':sid,'kind':'invalid_json','file':p.name,'detail':str(ex)});continue
    if 'a11y_tree' in p.name:
     filecounts['a11y_tree']+=1
     match=re.match(r'\d+_(.+\.exe)_(\d+)_a11y_tree.json',p.name,re.I)
     if match and isinstance(d,dict):trees.append((int(match[2])/1000,match[1],d.get('name','')))
    elif p.name.startswith('metadata_'):filecounts['metadata']+=1
    elif p.parent.name=='web_logs':
     filecounts['web_log']+=1;tab={'url':d.get('url',''),'title':d.get('title',''),'created':float(d.get('created_at',0))/1000,'events':d.get('interactions',[]),'file':p.name};tabs.append(tab);webraw+=len(tab['events'])
    elif isinstance(d,list):
     filecounts['desktop_log']+=1; raw+=len(d)
     if p.name not in meta_refs:issues.append({'session':sid,'kind':'unreferenced_desktop_log','file':p.name,'detail':len(d)})
     for i,e in enumerate(d):
      t=e.get('timestamp');app=(e.get('window') or {}).get('application') or re.sub(r'^\d+_|\.json$','',p.name)
      if not isinstance(t,(int,float)) or not math.isfinite(t):audit['invalid_desktop_timestamp']+=1;continue
      if not start<=t<=end:audit['desktop_outside_metadata_bounds']+=1
      desk.append({'timestamp':t,'event':e,'app':app,'file':p.name,'index':i})
   for ref in sorted(meta_refs):
    if not (folder/ref).exists():issues.append({'session':sid,'kind':'zero_event_metadata_reference_without_file' if not any(a.get('events',0)>0 for a in m['session']['applications'] if Path(a.get('log_file','')).name==ref) else 'missing_positive_event_log_reference','file':ref,'detail':'not present in fixed-revision session directory; actual session files scanned independently'})
   desk.sort(key=lambda x:(x['timestamp'],x['file'],x['index']))
   if raw!=meta_count:issues.append({'session':sid,'kind':'metadata_count_mismatch','file':mp.name,'detail':f'{meta_count} vs {raw}'})
   events=[];seen=set()
   for x in desk:
    source,evidence=semantic(x['event'],x['app'],tabs,trees);full_sources[source]+=1
    key=json.dumps(x['event'],sort_keys=True)
    if key in seen:audit['duplicate_desktop_events']+=1;continue
    seen.add(key)
    if not start<=x['timestamp']<=end:continue
    target=MAP.get(source,(None,None))[0]
    events.append({'session':sid,'timestamp':x['timestamp'],'source_app':source,'app_id':VOCAB[target] if target else '', 'app_name':target or '', 'origin':'desktop','event_type':etype(x['event']),'evidence':evidence,'file':x['file'],'event_index':x['index'],'window_title':(x['event'].get('window') or {}).get('title','')})
   desktops=list(events);times=[e['timestamp'] for e in desktops];webseen=set()
   for tab in tabs:
    for i,e in enumerate(tab['events']):
     web_audit['raw']+=1;t=e.get('timestamp');kind=e.get('type')
     if not isinstance(t,(int,float)):web_audit['invalid_timestamp']+=1;continue
     t=t/1000
     if not start<=t<=end:web_audit['outside_session']+=1;continue
     if kind=='blur':web_audit['blur_excluded']+=1;continue
     pos=bisect.bisect_right(times,t)-1
     if pos<0:web_audit['no_desktop_context']+=1;continue
     context=desktops[pos]
     if 'chrome' not in context['file'].lower() and 'msedge' not in context['file'].lower():web_audit['nonbrowser_foreground']+=1;continue
     source=web_source(tab['url'],tab['title'])
     if not source:web_audit['no_source']+=1;continue
     if source!=context['source_app'] and normalized(tab['title'])!=normalized(context['window_title']):web_audit['title_or_source_conflict']+=1;continue
     k=json.dumps([t,source,e],sort_keys=True)
     if k in webseen:web_audit['duplicate_web']+=1;continue
     webseen.add(k)
     near=desktops[max(0,bisect.bisect_left(times,t-.1)):bisect.bisect_right(times,t+.1)]
     if any(x['source_app']==source and x['event_type']==kind for x in near):web_audit['paired_desktop_duplicate']+=1;continue
     target=MAP.get(source,(None,None))[0]
     events.append({'session':sid,'timestamp':t,'source_app':source,'app_id':VOCAB[target] if target else '', 'app_name':target or '', 'origin':'web','event_type':kind,'evidence':'web_url_with_desktop_context','file':'web_logs/'+tab['file'],'event_index':i,'window_title':tab['title']});web_audit['accepted']+=1
   events.sort(key=lambda e:(e['timestamp'],e['origin']!='desktop',e['file'],e['event_index']))
   rawsw=mappedsw=collapsed=unmapped=0
   for a,b in zip(events,events[1:]):
    if a['source_app']==b['source_app']:continue
    rawsw+=1
    status='unmapped_endpoint' if not a['app_name'] or not b['app_name'] else 'collapsed' if a['app_name']==b['app_name'] else 'retained'
    mappedsw+=status=='retained';collapsed+=status=='collapsed';unmapped+=status=='unmapped_endpoint'
    all_switches.append({'session':sid,'timestamp':b['timestamp'],'source_from':a['source_app'],'source_to':b['source_app'],'target_from':a['app_name'],'target_to':b['app_name'],'status':status})
   # Every unmapped run is an explicit barrier. Never join A/UNKNOWN/A into dwell or reentry.
   segments=[]
   for e in events:
    key=e['app_name'] or '<UNKNOWN>'
    if segments and segments[-1]['app_name']==key:segments[-1]['last_event']=e['timestamp'];segments[-1]['events']+=1
    else:segments.append({'session':sid,'segment_index':len(segments),'start':e['timestamp'],'last_event':e['timestamp'],'end':end,'app_name':key,'app_id':e['app_id'],'events':1})
   for i in range(len(segments)-1):segments[i]['end']=segments[i+1]['start']
   samples,skipped=episodes(sid,segments,end)
   audit['departures_into_unknown_not_candidates']+=skipped
   for e in events:sources[e['source_app']][e['origin']]+=1
   sr={'session':sid,'user':f'SU{user}','task_id':task,'usable':int(bool(events) and end>=start),'task_success':m['task'].get('success'),'duration_s':end-start,'raw_desktop_events':raw,'metadata_desktop_events':meta_count,'raw_web_events':webraw,'merged_events':len(events),'mapped_events':sum(bool(e['app_name']) for e in events),'mapped_desktop_events':sum(bool(e['app_name']) for e in desktops),'source_apps':len(set(e['source_app'] for e in events)),'target_apps':len(set(e['app_name'] for e in events if e['app_name'])),'raw_switches':rawsw,'mapped_switches':mappedsw,'collapsed_switches':collapsed,'unmapped_switches':unmapped,'segments':len(segments),'reentry_events':sum(not x['censored'] for x in samples),'censored_candidates':sum(x['censored'] for x in samples)}
   if not sr['usable']:issues.append({'session':sid,'kind':'no_usable_interactions','file':mp.name,'detail':'no desktop or accepted web interaction events'})
   session_rows.append(sr);all_events+=events;all_segments+=segments;all_samples+=samples
 bins=collections.Counter(x['class'] for x in all_samples if not x['censored']); totals={k:sum(x[k] for x in session_rows) for k in ['usable','raw_desktop_events','metadata_desktop_events','raw_web_events','merged_events','mapped_events','mapped_desktop_events','raw_switches','mapped_switches','collapsed_switches','unmapped_switches','segments','reentry_events','censored_candidates']}
 totals.update(users=8,sessions=len(session_rows),unique_source_apps=len(sources),unique_target_apps=len(set(e['app_name'] for e in all_events if e['app_name'])),bins={f'C{i}':bins[f'C{i}'] for i in range(8)},mapping_coverage=totals['mapped_events']/totals['merged_events'],desktop_mapping_coverage=totals['mapped_desktop_events']/totals['raw_desktop_events'],switch_retention=totals['mapped_switches']/totals['raw_switches'],session_duration_min=min(durations),session_duration_median=statistics.median(durations),session_duration_max=max(durations),total_session_seconds=sum(durations))
 assert totals['raw_switches']==totals['mapped_switches']+totals['collapsed_switches']+totals['unmapped_switches']
 assert sum(bins.values())==totals['reentry_events'];assert len(all_samples)==totals['reentry_events']+totals['censored_candidates']
 assert len(session_rows)==480
 assert all(x['return_time']>x['leave_time'] for x in all_samples if not x['censored'])
 csvout('sessions.csv',session_rows);csvout('merged_events.csv',all_events);csvout('interaction_sequence.csv',all_segments);csvout('reentry_samples.csv',all_samples);csvout('switches.csv',all_switches);csvout('issues.csv',issues,['session','kind','file','detail']);csvout('su_file_hashes.csv',[dict(path=n,bytes=b,sha256=h) for n,b,h in hashes])
 source_rows=[]
 for source,count in sorted(sources.items()):
  target,cat=MAP.get(source,(None,'未映射'));source_rows.append({'source_app':source,'app_id':VOCAB[target] if target else '', 'app_name':target or '', 'category':cat,'desktop_events':count['desktop'],'web_events':count['web'],'total':sum(count.values())})
 csvout('source_app_mapping.csv',source_rows)
 payload={'revision':repo['sha'],'totals':totals,'web_merge_audit':dict(web_audit),'file_counts':dict(filecounts),'semantic_evidence_counts':dict(collections.Counter(e['evidence'] for e in all_events)),'raw_source_counts':dict(full_sources),'quality_audit':dict(audit),'issues':issues,'mapping':source_rows,'unmapped_sources':[x for x in source_rows if not x['app_name']],'censor_reasons':dict(collections.Counter(x['reason'] for x in all_samples if x['censored']))}
 (OUT/'statistics.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n');print(json.dumps(payload,ensure_ascii=False,indent=2))

def episodes(sid,segments,end):
 samples=[];pending={};skipped=0
 for i,s in enumerate(segments):
  current=s['app_name'];t=s['start']
  if current=='<UNKNOWN>':
   for a,leave in pending.items():samples.append(sample(sid,a,leave,None,t,'unknown_boundary'))
   pending={}
   if i and segments[i-1]['app_name']!='<UNKNOWN>':skipped+=1
   continue
  if current in pending:samples.append(sample(sid,current,pending.pop(current),t,t,'returned'))
  if i and segments[i-1]['app_name']!='<UNKNOWN>':pending[segments[i-1]['app_name']]=t
 for a,leave in pending.items():samples.append(sample(sid,a,leave,None,end,'session_end'))
 return samples,skipped

def sample(sid,app,leave,ret,until,reason):
 gap=ret-leave if ret is not None else None
 return {'session':sid,'app_id':VOCAB[app],'app_name':app,'leave_time':leave,'return_time':ret if ret is not None else '', 'reentry_s':gap if gap is not None else '', 'observed_until':until,'observed_s':until-leave,'class':f'C{bisect.bisect_left([30,60,180,300,600,1800,3600],gap)}' if gap is not None else '', 'censored':int(ret is None),'reason':reason}
if __name__=='__main__':run()
