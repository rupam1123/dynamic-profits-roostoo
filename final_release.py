"""Verify the final package and the exact installed V3 migration dependencies.

Read-only, offline; never reads credentials or trading state.
"""
import argparse
import hashlib
from pathlib import Path


def verify(manifest, project):
    total = 0
    for line in manifest.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        expected, name = line.split('  ', 1)
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise SystemExit('Unsafe release path')
        path = project / relative
        if not path.is_file():
            raise SystemExit('Required file missing: ' + name)
        actual = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        if actual != expected:
            raise SystemExit('File differs from the verified release: ' + name + '. Preserve local changes; inspect before installation.')
        total += 1
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--verify', action='store_true')
    modes.add_argument('--base-only', action='store_true')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    args = parser.parse_args()
    packaged = Path(__file__).resolve().parent
    total = verify(packaged/'FINAL_BASE_FILES.sha256', args.project)
    if args.verify:
        total += verify(packaged/'FINAL_FILES.sha256', args.project)
    print('FINAL_RELEASE_VERIFIED:', total, 'files. No network, account access or orders.')


if __name__ == '__main__':
    main()
