"""Commit-checked V3 -> V3.1 service migration. No discretionary orders."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from . import controller as bot, migrate

PROJECT = Path('/home/ssm-user/dynamic-profits-roostoo')
SERVICE = 'roostoo-competition'
OVERRIDE = Path('/etc/systemd/system/roostoo-competition.service.d/50-baseline-v31.conf')
OLD_MODULE = 'competition_v3.controller'
NEW_MODULE = 'competition_v31.controller'
UNIT = '[Service]\nExecStart=\nExecStart=/usr/bin/python3 -u -m ' + NEW_MODULE + ' --execute-competition --watch\n'


def run(*args, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check).stdout.strip()


def gate(root, repair=False):
    if root != PROJECT:
        raise RuntimeError('Run in the AWS project directory: ' + str(PROJECT))
    files = [str(p.relative_to(root)) for p in (root / 'competition_v31').iterdir() if p.suffix in ('.py', '.json')]
    run('git', 'ls-files', '--error-unmatch', *files)
    run('git', 'diff', '--exit-code', 'HEAD', '--', 'competition_v31', 'competition_v3')
    state = migrate.preflight(migrate.ReadOnlyStore(bot.ROOT / 'execution.sqlite3'))
    command = run('systemctl', 'show', SERVICE, '--property=ExecStart', '--value')
    active = run('systemctl', 'is-active', SERVICE, check=False) == 'active'
    if state['version'] == bot.VERSION:
        if not repair and NEW_MODULE in command and active:
            print('V31_ALREADY_INSTALLED. Inspect its journal and report; no changes made.')
            return False
        if not repair:
            raise RuntimeError('State is already migrated. Use --repair-service to finish the service switch; do not restore V3.')
        if OLD_MODULE not in command and NEW_MODULE not in command:
            raise RuntimeError('Unrecognized service configuration; inspect it before repair')
        return True
    if repair:
        raise RuntimeError('--repair-service only finishes an already migrated V3.1 journal')
    if OLD_MODULE not in command or NEW_MODULE in command or not active:
        raise RuntimeError('Expected the active V3 competition service')
    hour, minute = int(time.time()) // 3600, int(time.time() % 3600) // 60
    safely_paused = bot.paused(state, hour) and not state['positions']
    if state.get('flatten_pending') or not 16 <= minute <= 49 or (state['last_hour'] != hour and not safely_paused):
        raise RuntimeError('Upgrade only in UTC minutes 16-49 after the current cycle completes, or while flat and paused. Leave V3 running and retry then.')
    if OVERRIDE.exists():
        raise RuntimeError('A pre-existing V3.1 override needs inspection')
    return True


def write_override():
    run('sudo', '-n', 'mkdir', '-p', str(OVERRIDE.parent))
    subprocess.run(['sudo', '-n', 'tee', str(OVERRIDE)], input=UNIT, text=True,
        stdout=subprocess.DEVNULL, check=True)
    run('sudo', '-n', 'systemctl', 'daemon-reload')


def install(repair=False):
    root = Path.cwd().resolve()
    with bot.process_lock(bot.ROOT / 'upgrade.lock'):
        if not gate(root, repair):
            return
        commit = run('git', 'rev-parse', 'HEAD')
        run('sudo', '-n', 'true')
        run('sudo', '-n', 'systemctl', 'stop', SERVICE)
        try:
            with bot.process_lock(bot.ROOT / 'controller.lock'):
                store = bot.Store(bot.ROOT / 'execution.sqlite3')
                migrate.preflight(store)
                client = bot.credentials(Path.home() / '.config/dynamic-profits/competition.json')
                backup = migrate.migrate(client, store, bot.ROOT / 'backups')
                state = store.load()
                store.save(state, 'DEPLOYMENT', dict(commit=commit, module=NEW_MODULE, repair=repair))
                write_override()
            run('sudo', '-n', 'systemctl', 'start', SERVICE)
            print(json.dumps(dict(status='V31_SERVICE_STARTED', commit=commit,
                backup=str(backup) if backup else None,
                note='Confirm a new risk check/cycle in journalctl. Service start alone is not fill verification.')))
        except BaseException:
            # Inspect durable state; an exception can occur AFTER a successful commit.
            # Never restart old code over a migrated journal, even if the local flag was not set.
            try:
                version = migrate.ReadOnlyStore(bot.ROOT / 'execution.sqlite3').load().get('version')
            except Exception:
                version = None
            if version == migrate.OLD_VERSION and not OVERRIDE.exists():
                run('sudo', '-n', 'systemctl', 'start', SERVICE, check=False)
                print('State stayed V3. Original service restart requested; inspect status.')
            elif version == bot.VERSION:
                print('State is V3.1. Preserve journal/backups. Run this module with --repair-service after inspecting the error; never start V3 against this state.')
            else:
                print('State could not be safely identified. Service remains stopped; preserve all files and inspect before restarting.')
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--apply', action='store_true')
    modes.add_argument('--repair-service', action='store_true')
    args = parser.parse_args()
    install(args.repair_service)


if __name__ == '__main__':
    main()
