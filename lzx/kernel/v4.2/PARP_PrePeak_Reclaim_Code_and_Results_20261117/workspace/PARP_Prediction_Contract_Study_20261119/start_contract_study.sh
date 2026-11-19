#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
mkdir -p -- results
exec > >(tee -a results/launcher.log) 2>&1
python3 -B verify_source.py
python3 -B -m unittest -q test_contract
exec python3 -B -u stage_native.py
