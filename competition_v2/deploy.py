"""Apply a committed strategy release during a verified idle window. No discretionary orders."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from . import controller as bot
from . import migrate

SERVICE='roostoo-competition'
OVERRIDE=Path('/etc/systemd/system/roostoo-competition.service.d/20-architecture-v2.conf')


def run(*args,check=True):
    return subprocess.run(args,text=True,capture_output=True,check=check).stdout.strip()


def gate(root):
    if root != Path('/home/ssm-user/dynamic-profits-roostoo'):
        raise RuntimeError('Run in /home/ssm-user/dynamic-profits-roostoo on the AWS instance')
    files=[str(p.relative_to(root)) for p in (root/'competition_v2').iterdir() if p.suffix in ('.py','.json')]
    run('git','ls-files','--error-unmatch',*files)
    run('git','diff','--exit-code','HEAD','--','competition_v2','competition_bot')
    if not (bot.ROOT/'execution.sqlite3').is_file(): raise RuntimeError('Existing competition journal missing')
    state=migrate.preflight(bot.Store())
    command=run('systemctl','show',SERVICE,'--property=ExecStart','--value')
    if state['version']==bot.VERSION:
        if 'competition_v2.controller' not in command: raise RuntimeError('Migration exists but service differs; inspect configuration')
        print('V2_ALREADY_INSTALLED. Use journalctl and the report to check activity.')
        return False
    if 'competition_bot.controller' not in command or 'competition_v2.controller' in command:
        raise RuntimeError('Unexpected running controller; no service changes made')
    if run('systemctl','is-active',SERVICE)!='active': raise RuntimeError('Legacy service is not active; inspect it first')
    minute=int(time.time()%3600)//60
    if not 16<=minute<=49 or state['last_hour']!=int(time.time())//3600:
        raise RuntimeError('Upgrade only in UTC minutes 16–49 after the current hourly cycle completes. Leave the bot running and retry in that window.')
    if OVERRIDE.exists(): raise RuntimeError('Existing v2 service override needs review')
    return True


def install():
    root=Path.cwd().resolve()
    with bot.process_lock(bot.ROOT/'upgrade.lock'):
        if not gate(root): return
        commit=run('git','rev-parse','HEAD')
        # Ensure sudo is available BEFORE stopping the healthy service.
        run('sudo','-n','true')
        run('sudo','-n','systemctl','stop',SERVICE)
        migrated=False; wrote=False
        try:
            with bot.process_lock(bot.ROOT/'controller.lock'):
                store=bot.Store()
                migrate.preflight(store)
                client=bot.credentials(Path.home()/'.config/dynamic-profits/competition.json')
                backup=migrate.migrate(client,store,bot.ROOT/'backups')
                migrated=True
                state=store.load(); store.save(state,'DEPLOYMENT',dict(commit=commit,module='competition_v2.controller'))
                content='[Service]\nExecStart=\nExecStart=/usr/bin/python3 -u -m competition_v2.controller --execute-competition --watch\n'
                run('sudo','-n','mkdir','-p',str(OVERRIDE.parent))
                subprocess.run(['sudo','-n','tee',str(OVERRIDE)],input=content,text=True,stdout=subprocess.DEVNULL,check=True)
                wrote=True
                run('sudo','-n','systemctl','daemon-reload')
            run('sudo','-n','systemctl','start',SERVICE)
            print(json.dumps(dict(status='V2_SERVICE_STARTED',commit=commit,backup=str(backup),
                 note='Inspect journal and report for first v2 cycle. Service start alone is not fill verification.')))
        except Exception:
            if not migrated:
                # Nothing changed in the execution state: resume the original controller.
                run('sudo','-n','systemctl','start',SERVICE,check=False)
                print('Upgrade did not migrate. Original service restart requested; inspect status. No journal was reset.')
            else:
                print('State migrated to v2. Do NOT restore v1 or initialize. Inspect service configuration and logs; retain all backups.')
                if wrote: run('sudo','-n','systemctl','start',SERVICE,check=False)
            raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',required=True)
    parser.parse_args()
    install()

if __name__=='__main__': main()
