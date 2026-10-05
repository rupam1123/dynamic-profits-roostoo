"""Store testing credentials outside the repository, with restrictive file permissions."""
import getpass
import json
import os
from pathlib import Path
import warnings


def main():
    target = Path.home()/'.config/dynamic-profits/testing.json'
    if target.exists():
        raise SystemExit('Testing credentials already exist. No overwrite performed.')
    print('Use the SAME TESTING account as the verified USD 49,999.95 wallet.')
    print('This configures the 26-hour small-exposure controller; it does not place orders.')
    if input('Type TEST ACCOUNT: ').strip() != 'TEST ACCOUNT': raise SystemExit('Cancelled')
    warnings.simplefilter('error', getpass.GetPassWarning)
    key = getpass.getpass('Testing API key (hidden): ').strip()
    secret = getpass.getpass('Testing API secret (hidden): ').strip()
    if not key or not secret: raise SystemExit('Empty credentials')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'purpose':'TESTING', 'key':key, 'secret':secret}, f)
        f.flush(); os.fsync(f.fileno())
    print('TESTING_CREDENTIALS_SAVED. Next run --initialize-testing. No orders placed.')


if __name__ == '__main__': main()
