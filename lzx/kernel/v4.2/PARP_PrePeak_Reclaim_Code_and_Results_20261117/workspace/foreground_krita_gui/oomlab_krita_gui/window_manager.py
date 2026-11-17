import re
from .config import command,SafetyError


def parse_windows(text):
    rows=[]
    for line in text.splitlines():
        p=line.split(None,8)
        if len(p)==9:
            rows.append(dict(id=p[0],pid=int(p[2]),x=int(p[3]),y=int(p[4]),width=int(p[5]),height=int(p[6]),title=p[8]))
    return rows


class Windows:
    def all(self): return parse_windows(command(['wmctrl','-lpG']))

    def select(self,pid,previous):
        matches=[w for w in self.all() if w['pid']==pid and w['id'] not in previous and 'krita' in w['title'].lower()]
        # QSplashScreen shares PID and title with the real application window.
        # Binding it causes stale-window input as soon as startup completes.
        normal=[]
        for w in matches:
            kind=command(['xprop','-id',w['id'],'_NET_WM_WINDOW_TYPE'])
            if '_NET_WM_WINDOW_TYPE_NORMAL' in kind and not any(
                flag in kind for flag in ('_NET_WM_WINDOW_TYPE_SPLASH','_NET_WM_WINDOW_TYPE_UTILITY',
                                           '_NET_WM_WINDOW_TYPE_DIALOG')):
                normal.append(w)
        matches=normal
        if len(matches)!=1: raise RuntimeError(f'无法唯一识别本轮 Krita 窗口：{len(matches)}')
        return self.verify(matches[0]['id'],pid)

    def verify(self,wid,pid):
        rows=[w for w in self.all() if w['id']==wid and w['pid']==pid]
        if len(rows)!=1: raise SafetyError('窗口消失或 PID 错误')
        prop=command(['xprop','-id',wid,'_NET_WM_PID'])
        if not re.search(r'=\s*'+str(pid)+r'\s*$',prop): raise SafetyError('xprop PID 不匹配')
        info=command(['xwininfo','-id',wid])
        if 'IsViewable' not in info: raise RuntimeError('窗口不可见')
        # wmctrl positions can include window-manager gravity offsets. Use
        # actual client coordinates for screenshot crops and input anchors.
        geometry={}
        for key,label in [('x','Absolute upper-left X'),('y','Absolute upper-left Y'),
                          ('width','Width'),('height','Height')]:
            found=re.search(r'^\s*'+re.escape(label)+r':\s*(-?\d+)\s*$',info,re.M)
            if not found: raise RuntimeError('xwininfo 缺少客户区坐标：'+label)
            geometry[key]=int(found[1])
        return dict(rows[0],**geometry)

    def focus(self,wid,pid):
        self.verify(wid,pid); command(['xdotool','windowactivate','--sync',wid])
        self.assert_active(wid,pid)

    def assert_active(self,wid,pid,modal=False):
        active=command(['xdotool','getactivewindow']).strip()
        if not modal and int(active)!=int(wid,16): raise SafetyError('焦点不是目标窗口')
        prop=command(['xprop','-id',active,'_NET_WM_PID'])
        if not re.search(r'=\s*'+str(pid)+r'\s*$',prop): raise SafetyError('输入目标不属于本轮 PID')

    def active_box(self,wid,pid):
        self.assert_active(wid,pid,modal=True)
        active=command(['xdotool','getactivewindow']).strip()
        data=command(['xwininfo','-id',active])
        if 'IsViewable' not in data: raise RuntimeError('活动对话框不可见')
        values={}
        for key,label in [('x','Absolute upper-left X'),('y','Absolute upper-left Y'),('width','Width'),('height','Height')]:
            found=re.search(r'^\s*'+re.escape(label)+r':\s*(-?\d+)\s*$',data,re.M)
            if not found: raise RuntimeError('对话框缺少实际坐标')
            values[key]=int(found[1])
        return values

    def popup_boxes(self,wid,pid):
        self.verify(wid,pid); self.assert_active(wid,pid)
        tree=command(['xwininfo','-root','-tree'])
        result=[]
        for popup in re.findall(r'^\s{5}(0x[0-9a-fA-F]+)\s',tree,re.M):
            prop=command(['xprop','-id',popup,'_NET_WM_PID','_NET_WM_WINDOW_TYPE'])
            if not re.search(r'_NET_WM_PID\(CARDINAL\)\s*=\s*'+str(pid)+r'\s*$',prop,re.M): continue
            if not any(t in prop for t in ('_NET_WM_WINDOW_TYPE_POPUP_MENU','_NET_WM_WINDOW_TYPE_DROPDOWN_MENU')): continue
            info=command(['xwininfo','-id',popup])
            if 'IsViewable' not in info: continue
            values={}
            for key,label in [('x','Absolute upper-left X'),('y','Absolute upper-left Y'),('width','Width'),('height','Height')]:
                found=re.search(r'^\s*'+re.escape(label)+r':\s*(-?\d+)\s*$',info,re.M)
                if not found: raise RuntimeError('弹出菜单缺少实际坐标')
                values[key]=int(found[1])
            result.append(dict(id=popup,**values))
        if not result: raise RuntimeError('没有本轮 Krita 的可见弹出菜单')
        return result

    def title_point(self,wid,pid):
        w=self.verify(wid,pid)
        ext=command(['xprop','-id',wid,'_NET_FRAME_EXTENTS'])
        vals=[int(n) for n in ext.split('=',1)[-1].replace(',',' ').split()]
        if len(vals)!=4 or vals[2]<4: raise RuntimeError('无法可靠定位窗口标题栏')
        return w['x']+w['width']//2,w['y']-vals[2]//2

    def maximized(self,wid,pid):
        w=self.verify(wid,pid); state=command(['xprop','-id',wid,'_NET_WM_STATE'])
        desktop=int(command(['xprop','-root','_NET_CURRENT_DESKTOP']).split('=')[-1].strip())
        raw=command(['xprop','-root','_NET_WORKAREA']).split('=')[-1]
        values=[int(v) for v in raw.replace(',',' ').split()]; area=values[desktop*4:desktop*4+4]
        if len(area)!=4: raise RuntimeError('桌面工作区域不可用')
        ok=all(s in state for s in ['_NET_WM_STATE_MAXIMIZED_VERT','_NET_WM_STATE_MAXIMIZED_HORZ'])
        ok=ok and w['width']>=area[2]*.9 and w['height']>=area[3]*.85
        return dict(ok=ok,window=w,workarea=area)
