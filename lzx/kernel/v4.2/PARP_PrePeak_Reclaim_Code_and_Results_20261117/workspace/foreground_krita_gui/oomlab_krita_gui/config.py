import hashlib
import json
import math
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


class SafetyError(RuntimeError):
    """Identity/focus loss is fatal, never retried as a visual loading delay."""


def save(path,value):
    p=Path(path); tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n'); tmp.replace(p)


def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024**2),b''): h.update(b)
    return h.hexdigest()


def command(args,timeout=10,env=None):
    p=subprocess.run([str(a) for a in args],capture_output=True,text=True,timeout=timeout,env=env)
    if p.returncode: raise RuntimeError(f'{args[0]} exit={p.returncode}: {p.stderr.strip()}')
    return p.stdout


def validate(c):
    expected={'schema_version','display','asset','search_dirs','minimum_pixels','timeouts','poll_s',
              'stable_observations','ocr_language','ocr_confidence','zoom_scroll_ticks','change_threshold','regions','labels'}
    if set(c)!=expected or c['schema_version']!=1: raise ValueError('未知或缺失配置字段')
    if c['display'] is not None and (not isinstance(c['display'],str) or not c['display']): raise ValueError('display 无效')
    if set(c['asset'])!={'path','sha256'}: raise ValueError('asset 字段无效')
    if (c['asset']['path'] is None)!=(c['asset']['sha256'] is None): raise ValueError('素材路径与摘要须同时设置')
    if c['asset']['sha256'] is not None:
        import re
        if not re.fullmatch('[0-9a-f]{64}',c['asset']['sha256']): raise ValueError('SHA-256 无效')
        if not Path(c['asset']['path']).is_absolute(): raise ValueError('素材必须为绝对路径')
    for key in ['minimum_pixels','stable_observations','zoom_scroll_ticks']:
        if type(c[key]) is not int or c[key]<=0: raise ValueError(key+' 必须为正整数')
    if not 2<=c['stable_observations']<=10 or not 1<=c['zoom_scroll_ticks']<=10: raise ValueError('稳定次数或滚轮幅度越界')
    if set(c['timeouts'])!={'launch','load','action','close','exit'}: raise ValueError('超时字段不符')
    for v in [c['poll_s'],*c['timeouts'].values()]:
        if type(v) not in (int,float) or not math.isfinite(v) or v<=0: raise ValueError('时间必须有限且为正')
    if not 0.01<=c['poll_s']<=5 or max(c['timeouts'].values())>7200: raise ValueError('时间越界')
    if not 0<c['change_threshold']<1 or not 0<=c['ocr_confidence']<=100: raise ValueError('阈值越界')
    if not c['search_dirs'] or not all(isinstance(p,str) for p in c['search_dirs']): raise ValueError('搜索目录无效')
    if set(c['regions'])!={'canvas','status','layers','dialog','menu','popup'}: raise ValueError('区域字段不符')
    for box in c['regions'].values():
        if len(box)!=4 or any(type(x) not in (int,float) or not math.isfinite(x) for x in box): raise ValueError('区域格式无效')
        x,y,w,h=box
        if min(x,y)<0 or min(w,h)<=0 or x+w>1.00001 or y+h>1.00001: raise ValueError('区域必须在窗口内')
    for key in ['file_dialog','layer_panel','layer_menu','new_submenu','paint_layer','discard','save_prompt','empty_workspace']:
        if not c['labels'].get(key) or not all(isinstance(s,str) and s for s in c['labels'][key]): raise ValueError('OCR 标签缺失')
    return c


def load(path): return validate(json.loads(Path(path).read_text()))
