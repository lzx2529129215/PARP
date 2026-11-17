"""Read only existing ORA metadata and bytes. Never generate, extract or rewrite it."""
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from .config import sha256,save


def inspect(path):
    p=Path(path).expanduser().resolve(strict=True)
    if p.suffix.lower()!='.ora' or not p.is_file(): raise ValueError('不是现有 ORA 文件')
    with zipfile.ZipFile(p) as z:
        if z.getinfo('stack.xml').file_size>4*1024**2: raise ValueError('ORA XML 过大')
        if z.read('mimetype')!=b'image/openraster': raise ValueError('ORA mimetype 无效')
        raw=z.read('stack.xml')
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper(): raise ValueError('拒绝 XML 实体')
        root=ET.fromstring(raw); layers=root.findall('.//layer')
        w,h=int(root.attrib['w']),int(root.attrib['h'])
        if min(w,h)<=0 or not layers: raise ValueError('ORA 尺寸或图层无效')
        for layer in layers:
            src=layer.get('src','')
            if not src or src.startswith('/') or '..' in Path(src).parts or src not in z.namelist(): raise ValueError('图层文件引用无效')
    return dict(path=str(p),sha256=sha256(p),width=w,height=h,layers=len(layers),
                layer_names=[l.get('name','') for l in layers],bytes=p.stat().st_size)


def discover(config):
    rows=[]; seen=set()
    for directory in config['search_dirs']:
        d=Path(directory).expanduser()
        if not d.is_dir(): continue
        for path in sorted(d.rglob('*.ora')):
            if path.resolve() in seen: continue
            seen.add(path.resolve())
            try:
                row=inspect(path)
                small=bool(re.search(r'smoke',path.name,re.I)) or row['width']*row['height']<config['minimum_pixels']
                row.update(eligible=not small,reason='排除 smoke 名称或低于像素阈值' if small else '元数据候选；非实测内存结论')
            except Exception as e: row=dict(path=str(path),eligible=False,error=str(e))
            rows.append(row)
    return rows


def pin(config,rows,output):
    candidates=[r for r in rows if r.get('eligible')]
    if len(candidates)!=1: raise ValueError(f'无法唯一确认目标素材，候选数={len(candidates)}；请检查候选清单')
    p=Path(output)
    if p.exists(): raise FileExistsError('拒绝覆盖已有配置')
    config=dict(config,asset={k:candidates[0][k] for k in ['path','sha256']})
    save(p,config)
    return candidates[0]
