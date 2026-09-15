import time,subprocess,os
from common import *

def run(script,*args):
    label='-'.join([Path(script).stem,*args]);write(OUT/'progress.json',{'phase':label,'status':'RUNNING','kernel':'NOT RUN'})
    with (OUT/f'logs/{label}.log').open('w') as log:subprocess.run([sys.executable,str(ROOT/'root_cause'/script),*args],stdout=log,stderr=subprocess.STDOUT,check=True)

def main():
    start=time.monotonic()
    while True:
        p=OUT/'metrics/switch-progress.json';b=OUT/'dataset/original/validation.json'
        ready=p.exists() and json.loads(p.read_text()).get('status')=='PASS';built=b.exists() and json.loads(b.read_text()).get('status')=='PASS'
        if ready and built:break
        write(OUT/'progress.json',{'phase':'waiting for switch training and original dataset','switch':json.loads(p.read_text()) if p.exists() else 'starting','original_dataset_ready':built,'status':'RUNNING','kernel':'NOT RUN'})
        if time.monotonic()-start>14400:raise TimeoutError('prerequisites not complete; inspect existing logs')
        time.sleep(10)
    run('eval_variant.py','switch');run('train_variant.py','original');run('eval_variant.py','original');run('compare_original.py');run('report.py')
if __name__=='__main__':
    try:main()
    except Exception as e:write(OUT/'progress.json',{'status':'FAILED','error':repr(e),'kernel':'NOT RUN'});raise
