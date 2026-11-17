import json
import logging
import time
from .config import save,SafetyError

STATES=('LAUNCH_KRITA','VERIFY_KRITA','OPEN_ORA','VERIFY_DOCUMENT','MAXIMIZE_WINDOW','VERIFY_MAXIMIZE',
        'ZOOM_IN','VERIFY_ZOOM_IN','ZOOM_OUT','VERIFY_ZOOM_OUT','CREATE_LAYER','VERIFY_LAYER',
        'CLOSE_DOCUMENT','VERIFY_DISCARD','QUIT_KRITA','VERIFY_EXIT')


def wait_for(probe,timeout,poll,stable=2):
    deadline=time.monotonic()+timeout; count=0; last=None; error=None
    while True:
        try:
            last=probe()
            count=count+1 if last.get('ok') else 0
            if count>=stable: return last
        except SafetyError: raise
        except (RuntimeError,ValueError) as e: error=str(e); count=0
        if time.monotonic()>=deadline: raise TimeoutError(f'验证超时，最后观察={last}, error={error}')
        time.sleep(min(poll,max(0,deadline-time.monotonic())))


class Machine:
    def __init__(self,controller,run,metadata): self.g=controller; self.run=run; self.meta=metadata

    def execute(self):
        success=False
        startup_only=self.meta.get('startup_only') is True
        for index,name in enumerate(STATES[:2] if startup_only else STATES,1):
            logging.info('stage_start %s',name)
            row=dict(stage=name,stage_start=time.time_ns(),monotonic_start_ns=time.monotonic_ns(),
                     stage_end=None,success=False,verification=None,screenshot_before=None,screenshot_after=None,error=None)
            row['timestamp']=row['stage_start']
            try:
                row['screenshot_before']=self.g.capture(f'{index:02d}_{name}_before.png')
                row['verification']=getattr(self.g,name.lower())()
                if not row['verification'] or row['verification'].get('ok') is not True: raise RuntimeError('阶段未验证成功')
                row['screenshot_after']=self.g.capture(f'{index:02d}_{name}_after.png')
                row['success']=True
            except Exception as e:
                row['error']=repr(e); logging.exception('stage %s failed',name)
                try: row['screenshot_after']=self.g.capture(f'{index:02d}_{name}_error.png')
                except Exception as capture_error: row['capture_error']=repr(capture_error)
                try: save(self.run/'failure-windows.json',self.g.windows.all())
                except Exception as capture_error: row['window_error']=repr(capture_error)
            finally:
                row.update(stage_end=time.time_ns(),monotonic_end_ns=time.monotonic_ns())
                with open(self.run/'stages.jsonl','a') as f: f.write(json.dumps(row,ensure_ascii=False)+'\n')
                logging.info('stage_end %s success=%s error=%s',name,row['success'],row['error'])
            if not row['success']:
                self.meta.update(status='FAILED',error=row['error'],failed_stage=name,scene_preserved=True)
                break
        else:
            success=True
            self.meta.update(status='STARTUP_VERIFIED' if startup_only else 'COMPLETE',error=None,scene_preserved=startup_only)
        self.meta.update(success=success and not startup_only,ended_wall_ns=time.time_ns())
        save(self.run/'metadata.json',self.meta)
        return success
