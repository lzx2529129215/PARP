import subprocess,tarfile,json,hashlib,re
from pathlib import Path
B=Path(__file__).parent;ROOT=B/'Reduced-A11y-CUA';info=json.loads((B/'repository.json').read_text());rev=info['sha'];p=subprocess.Popen(['git','-C',str(B/'reduced-git'),'archive',rev],stdout=subprocess.PIPE);lfs=[];regular=0;written=0;validated=0
with tarfile.open(fileobj=p.stdout,mode='r|') as tar:
 for member in tar:
  if not member.isfile():continue
  data=tar.extractfile(member).read();dest=ROOT/member.name
  if data.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
   text=data.decode();sha=re.search(r'oid sha256:([0-9a-f]+)',text)[1];size=int(re.search(r'size (\d+)',text)[1]);lfs.append({'path':member.name,'sha256':sha,'bytes':size})
   if dest.exists():assert dest.stat().st_size==size and hashlib.sha256(dest.read_bytes()).hexdigest()==sha,member.name;validated+=1
   continue
  regular+=1
  if dest.exists():assert dest.read_bytes()==data,member.name;validated+=1
  else:dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data);written+=1
assert p.wait()==0
result={'revision':rev,'regular_files':regular,'written_from_git':written,'existing_verified':validated,'lfs_files':lfs,'missing_lfs':[x for x in lfs if not (ROOT/x['path']).exists()]};(B/'git_integrity.json').write_text(json.dumps(result,indent=2));print('regular',regular,'written',written,'LFS',len(lfs),'missing LFS',len(result['missing_lfs']))
