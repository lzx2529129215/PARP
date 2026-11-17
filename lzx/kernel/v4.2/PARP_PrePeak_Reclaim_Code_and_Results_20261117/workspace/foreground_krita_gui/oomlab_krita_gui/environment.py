import importlib.util
import os
import platform
import shutil
from .config import command
from .process_manager import qt_environment,require_x11_platform


def check(c):
    errors=[]; info=dict(gui_test='NOT_RUN',display=os.environ.get('DISPLAY'),platform=platform.platform(),
                         xauthority_present=bool(os.getenv('XAUTHORITY')),runtime_present=bool(os.getenv('XDG_RUNTIME_DIR')),
                         session_bus_present=bool(os.getenv('DBUS_SESSION_BUS_ADDRESS')))
    if os.geteuid()==0: errors.append('必须使用普通桌面用户')
    if not os.getenv('DISPLAY'): errors.append('缺少 X11 DISPLAY')
    if c['display'] and c['display']!=os.getenv('DISPLAY'): errors.append('配置 DISPLAY 与已授权会话不一致；不会擅自切换')
    krita_env,info['qt_environment']=qt_environment()
    try: require_x11_platform(krita_env)
    except RuntimeError as e: errors.append(str(e))
    for name in ['krita','xdpyinfo','wmctrl','xdotool','xprop','xwininfo','tesseract']:
        if not shutil.which(name): errors.append('缺少工具：'+name)
    for name in ['pyautogui','cv2','PIL']:
        if importlib.util.find_spec(name) is None: errors.append('缺少 Python 依赖：'+name)
    try:
        info['x11']=command(['xdpyinfo'])[:1500]
    except Exception as e: errors.append('X11 授权失败：'+str(e))
    try:
        require_x11_platform(krita_env)
        version=command(['krita','--version'],env=krita_env); info['krita_version']=version.strip()
        import re
        if not re.search(r'\bkrita\s+(?:5\.0\.2|5\.2\.2)\b',version,re.I):
            errors.append('本模块适配 Krita 5.0.2 / 5.2.2；实际独立 PID/窗口仍须逐轮验证')
    except Exception as e: errors.append('Krita 版本检查失败：'+str(e))
    try:
        langs=command(['tesseract','--list-langs'])
        if any(lang not in langs.splitlines() for lang in c['ocr_language'].split('+')): errors.append('OCR 语言包缺失')
    except Exception as e: errors.append(str(e))
    info.update(ok=not errors,errors=errors)
    return info
