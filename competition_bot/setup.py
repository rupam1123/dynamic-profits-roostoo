"""Save official competition credentials separately; no orders are submitted."""
import getpass
import json
import os
from pathlib import Path
import warnings


def main():
    target=Path.home()/'.config/dynamic-profits/competition.json'
    if target.exists():
        raise SystemExit('Competition credentials already exist; not overwritten. Use --initialize-competition only if state is new, otherwise --check-competition when service is not running.')
    print('Use the OFFICIAL competition key, not the USD 50,000 testing key.')
    print('Policy: hourly signals; 10% of starting USD per pair; at most 3 positions; 3% initial-capital drawdown trigger.')
    print('There is no 26-hour test cutoff. This configuration step places no orders.')
    if input('Type COMPETITION ACCOUNT: ').strip()!='COMPETITION ACCOUNT': raise SystemExit('Cancelled')
    warnings.simplefilter('error',getpass.GetPassWarning)
    key=getpass.getpass('Competition API key (hidden): ').strip()
    secret=getpass.getpass('Competition API secret (hidden): ').strip()
    if not key or not secret: raise SystemExit('Empty credentials')
    test=target.with_name('testing.json')
    if test.exists() and json.loads(test.read_text()).get('key')==key:
        raise SystemExit('This is the saved testing key. Competition configuration was not written.')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as handle:
        json.dump({'purpose':'COMPETITION','key':key,'secret':secret},handle)
        handle.flush(); os.fsync(handle.fileno())
    print('COMPETITION_CREDENTIALS_SAVED. No orders placed.')


if __name__=='__main__': main()
