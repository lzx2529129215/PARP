"""Explicit swap-backed/file-LRU basis; opt-in, legacy counters remain available."""
import os

def enabled():return os.environ.get('PARP_SERVICE_TYPED_LRU_BASIS','0')=='1'

def resident_view(stat):
 if not enabled():return stat
 keys=('inactive_anon','active_anon','inactive_file','active_file')
 if not all(k in stat for k in keys):raise ValueError('LRU type accounting unavailable')
 return {**stat,'raw_anon':stat.get('anon',0),'raw_file':stat.get('file',0),
  'anon':stat['inactive_anon']+stat['active_anon'],'file':stat['inactive_file']+stat['active_file'],'_lru_type_basis':1}

def mapped_anon_bounds(record):
 rss=record.get('Rss',0);anon=min(rss,record.get('Anonymous',0))
 if not enabled():return anon,anon
 lazy=min(anon,record.get('LazyFree',0));base=anon-lazy;remaining=rss-anon
 path=record.get('__path','');shmem=record.get('Pss_Shmem',0);file=record.get('Pss_File',0)
 known_shmem=path.startswith(('/dev/shm/','/dev/zero','/memfd:','/SYSV','[anon_shmem:'))
 if known_shmem or (shmem>0 and file==0):return base+remaining,base+remaining
 if shmem>0:return base,base+remaining
 return base,base

def compatible_reuse_state(state):
 if not isinstance(state,dict):return {}
 result=dict(state);basis=result.pop('__typed_basis',None)
 expected='lru-v1' if enabled() else 'mapped-v0'
 if basis is None and not enabled():return result
 return result if basis==expected else {}

def saved_reuse_state(state):
 return {**state,'__typed_basis':'lru-v1'} if enabled() else state
