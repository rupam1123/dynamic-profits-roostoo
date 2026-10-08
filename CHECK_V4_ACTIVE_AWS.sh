#!/usr/bin/env bash
set -euo pipefail
cd /home/ssm-user/dynamic-profits-roostoo
python3 verify_v4_active.py
python3 -m unittest test_competition_v4a test_v4a_selection test_v4a_runtime test_v4a_deploy test_v4a_management test_v4a_fast_orders test_v4a_activity -q
python3 -m competition_v4a.controller --preview
python3 -m competition_v4a.migrate --preflight
echo 'V4 checks complete. To switch the existing service: python3 -m competition_v4a.deploy --apply'
