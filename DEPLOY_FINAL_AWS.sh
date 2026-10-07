#!/usr/bin/env bash
set -euo pipefail
cd /home/ssm-user/dynamic-profits-roostoo
python3 final_release.py --verify
python3 -m unittest test_competition_v31 test_v31_deploy -q
python3 -m competition_v31.controller --preview
python3 -m competition_v31.migrate --preflight
python3 -m competition_v31.deploy --apply
systemctl show roostoo-competition -p ExecStart -p ActiveState --no-pager
sudo journalctl -u roostoo-competition -n 30 --no-pager
python3 -m competition_v31.report
