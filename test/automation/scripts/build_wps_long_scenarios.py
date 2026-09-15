"""Build isolated inputs and scenarios without launching a GUI or downloading."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from automation.wps_long_scenarios import build

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',default='outputs/executable_wps_long_v1')
    a=p.parse_args();print(json.dumps(build(a.output),ensure_ascii=False,indent=2))
