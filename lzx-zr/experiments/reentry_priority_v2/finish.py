"""Complete offline phases 5–7 after both training jobs finish; then STOP."""
from common import *
import time,subprocess
while not all((OUT/m/'status.json').exists() for m in CFG['sampling_modes']):time.sleep(5)
for name in ('evaluate','a11y','verify','gate','report'):
 print('START',name,flush=True)
 with (ROOT/f'{name}.log').open('w') as f:subprocess.run([sys.executable,str(ROOT/f'{name}.py')],stdout=f,stderr=subprocess.STDOUT,check=True)
 print('DONE',name,flush=True)
print('STOP',flush=True)
