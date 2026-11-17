import csv
import io
import re
import subprocess


def parse_tsv(text,confidence):
    rows=[]
    for r in csv.DictReader(io.StringIO(text),delimiter='\t'):
        try:
            if float(r['conf'])>=confidence and r['text'].strip():
                rows.append(dict(text=r['text'].strip(),x=int(r['left']),y=int(r['top']),
                                 w=int(r['width']),h=int(r['height']),line=tuple(r[k] for k in ['block_num','par_num','line_num'])))
        except (KeyError,ValueError): continue
    return rows


def find_phrase(words,labels):
    matches=[]
    for label in labels:
        wanted=label.casefold().split()
        for i in range(len(words)-len(wanted)+1):
            part=words[i:i+len(wanted)]
            if len({w['line'] for w in part})!=1: continue
            if any(b['x']-(a['x']+a['w'])>3*max(a['h'],b['h']) for a,b in zip(part,part[1:])): continue
            actual=[w['text'].casefold().strip(':&') for w in part]
            if actual!=wanted: continue
            x=min(w['x'] for w in part); y=min(w['y'] for w in part)
            right=max(w['x']+w['w'] for w in part); bottom=max(w['y']+w['h'] for w in part)
            matches.append(dict(text=label,x=x,y=y,w=right-x,h=bottom-y))
    # Synonym labels can point to exactly the same text box.
    return list({(m['x'],m['y'],m['w'],m['h']):m for m in matches}.values())


def zoom(text):
    values=[float(v.replace(',','.')) for v in re.findall(r'(\d+(?:[.,]\d+)?)\s*%',text)]
    if len(values)!=1 or not 0<values[0]<=10000: raise ValueError('缩放比例 OCR 不唯一或无效')
    return values[0]


def dimensions(text,w,h):
    # Krita's English status bar groups pixel dimensions, e.g. 4,096 x 4,096.
    text=re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))','',text)
    return bool(re.search(r'(?<!\d)'+str(w)+r'\s*[x×]\s*'+str(h)+r'(?!\d)',text,re.I))


class Visual:
    def __init__(self,config): self.c=config

    def words(self,image,scale=2,psm=11):
        # Desktop menu fonts are often 12-15px. Upscale screenshots for OCR,
        # then map every box back to the original input coordinates.
        buf=io.BytesIO(); image.resize((image.width*scale,image.height*scale)).save(buf,format='PNG')
        p=subprocess.run(['tesseract','stdin','stdout','-l',self.c['ocr_language'],'--psm',str(psm),'tsv'],
                         input=buf.getvalue(),capture_output=True,timeout=20)
        if p.returncode: raise RuntimeError('Tesseract 失败：'+p.stderr.decode(errors='replace'))
        words=parse_tsv(p.stdout.decode(),self.c['ocr_confidence'])
        for word in words:
            word['x']//=scale; word['y']//=scale
            word['w']=(word['w']+scale-1)//scale; word['h']=(word['h']+scale-1)//scale
        return words

    def text(self,image): return ' '.join(w['text'] for w in self.words(image))

    def unique(self,image,labels):
        matches=find_phrase(self.words(image),labels)
        if not matches:
            matches=find_phrase(self.words(image,scale=3),labels)
        if not matches:
            matches=find_phrase(self.words(image,scale=3,psm=6),labels)
        if len(matches)!=1: raise RuntimeError('视觉锚点不唯一：'+str(labels))
        return matches[0]

    def changed(self,before,after):
        import cv2
        import numpy as np  # Only screenshot comparison, never an image-processing workload.
        a=cv2.cvtColor(np.asarray(before),cv2.COLOR_RGB2GRAY)
        b=cv2.cvtColor(np.asarray(after),cv2.COLOR_RGB2GRAY)
        if a.shape!=b.shape: raise ValueError('截图区域尺寸改变')
        diff=cv2.absdiff(a,b); fraction=float((diff>12).mean())
        return dict(changed=fraction>=self.c['change_threshold'],fraction=fraction)

    def selected(self,image,box):
        import cv2
        import numpy as np
        y=max(0,box['y']-3); bottom=min(image.height,box['y']+box['h']+3)
        hsv=cv2.cvtColor(np.asarray(image.crop((0,y,image.width,bottom))),cv2.COLOR_RGB2HSV)
        # Conservative blue selection evidence. Unsupported themes fail, never assume.
        ratio=float(((hsv[:,:,0]>=85)&(hsv[:,:,0]<=125)&(hsv[:,:,1]>50)&(hsv[:,:,2]>40)).mean())
        return dict(selected=ratio>.25,blue_fraction=ratio)
