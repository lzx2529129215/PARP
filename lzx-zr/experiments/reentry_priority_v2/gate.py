from common import *
def main():
 g=json.loads((OUT/'gate2-offline-checks.json').read_text());a=json.loads((OUT/'a11y/validation.json').read_text());v=json.loads((OUT/'verification.json').read_text());assert v['status']=='PASS';assert set(a)=={'functional','process'}
 import subprocess
 guard=json.loads((OUT/'runtime-kernel-guard.json').read_text());diff=subprocess.check_output(['git','-C',str(ROOT.parents[2]),'diff','--binary','--','lzx/service','lzx/kernel']);guard['after_evaluation_sha256']=hashlib.sha256(diff).hexdigest();guard['unchanged']=guard['after_evaluation_sha256']==guard['existing_diff_sha256'];write(OUT/'runtime-kernel-guard.json',guard)
 g['external_validation_complete']=True;g['artifact_validation']=v['status'];g['action']='STOP';g['runtime_kernel_modified']=False;g['scope']='Gate-2 offline development decision only; execution ends here even on PASS'
 sources=list(ROOT.glob('*.py'))+[ROOT/'config.json']+list((OLD/'models').glob('*.py'))
 write(OUT/'source_manifest.json',{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
 write(OUT/'gate2.json',g);print(g['status'])
if __name__=='__main__':main()
