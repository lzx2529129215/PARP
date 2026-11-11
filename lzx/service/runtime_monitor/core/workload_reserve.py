"""Transfer the same parent's forecast allowance to background admission.

Resident protected grants are already in current charge and are not added.
"""
from pathlib import Path
import threading,time
class WorkloadReserve:
 def __init__(self,clock=time.monotonic,stat=None):
  self.clock=clock;self.stat=stat or (lambda p:Path(p).stat());self.lock=threading.RLock();self.rows={}
 def update(self,result):
  now=self.clock();new={};app=result.get('foreground','')
  health=result.get('io_accounting',{})
  if result.get('error') or result.get('low_errors') or (health.get('required') and not health.get('ready')):app=''
  if app and app!='UNKNOWN':
   for parent,plan in result.get('shared_budget',{}).items():
    try:
     s=self.stat(parent);capacity=max(0,int(plan['capacity']));amount=min(capacity,max(0,int(plan.get('reserve',0)))+max(0,int(plan.get('operation_workspace_requested',0))))
     new[parent]=(app,(s.st_dev,s.st_ino),now,amount,capacity)
    except (OSError,ValueError,KeyError,TypeError):continue
  with self.lock:self.rows=new
 def get(self,parent,identity,app):
  with self.lock:r=self.rows.get(parent)
  if r is None:return dict(bytes=0,source='no_recent_shared_budget')
  fg,generation,stamp,amount,capacity=r;age=self.clock()-stamp
  if fg!=app or generation!=tuple(identity) or not 0<=age<6:return dict(bytes=0,source='different_or_stale_shared_budget')
  return dict(bytes=amount,capacity_bytes=capacity,source='shared_parent_forecast_allowance',age_ms=age*1000,parent=parent,parent_identity=list(generation),foreground=fg)