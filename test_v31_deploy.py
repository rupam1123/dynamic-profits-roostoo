"""Offline deployment interruption, repair and real adapter deadline checks."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from competition_v31 import api, controller as bot, deploy, migrate
from competition_v3 import controller as old
from v3_tests.exchange import Exchange, MIDNIGHT


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'execution.sqlite3'
        self.override = self.root / '50-baseline-v31.conf'
        self.ex = Exchange()
        self.ex.clock = MIDNIGHT + 20*60
        self.commands = []
        for context in (patch.object(bot, 'ROOT', self.root), patch.object(deploy, 'OVERRIDE', self.override),
                patch.object(bot.time, 'time', side_effect=lambda:self.ex.clock),
                patch.object(bot, 'emit'), patch.object(old, 'emit'), patch('builtins.print')):
            context.start(); self.addCleanup(context.stop)
        old.initialize(self.ex, old.Store(self.database), self.ex.clock)
        state = bot.Store(self.database).load()
        state['last_hour'] = int(self.ex.clock)//3600
        bot.Store(self.database).save(state, 'TEST', {})

    def command(self, *args, **kw):
        self.commands.append(args)
        if args[:2] == ('git', 'rev-parse'):
            return 'offline-committed-release'
        if args[:2] == ('systemctl', 'show'):
            return '/usr/bin/python3 -u -m ' + deploy.OLD_MODULE + ' --execute-competition --watch'
        if args[:2] == ('systemctl', 'is-active'):
            return 'active'
        return ''

    def write_override(self):
        self.override.write_text(deploy.UNIT)

    @contextmanager
    def installation(self):
        with patch.object(deploy, 'gate', return_value=True), patch.object(deploy, 'run', side_effect=self.command), \
                patch.object(bot, 'credentials', return_value=self.ex), \
                patch.object(deploy, 'write_override', side_effect=self.write_override):
            yield

    def starts(self):
        return [c for c in self.commands if c == ('sudo', '-n', 'systemctl', 'start', deploy.SERVICE)]

    def test_success_changes_one_service_and_preserves_account(self):
        before = self.ex.balance()
        with self.installation():
            deploy.install()
        self.assertEqual(bot.Store(self.database).load()['version'], bot.VERSION)
        self.assertIn(deploy.NEW_MODULE, self.override.read_text())
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.ex.balance(), before)
        self.assertEqual(self.ex.writes, 0)

    def test_failure_before_state_commit_requests_original_restart(self):
        with self.installation(), patch.object(migrate, 'migrate', side_effect=RuntimeError('read failed')):
            with self.assertRaisesRegex(RuntimeError, 'read failed'):
                deploy.install()
        self.assertEqual(bot.Store(self.database).load()['version'], old.VERSION)
        self.assertEqual(len(self.starts()), 1)
        self.assertFalse(self.override.exists())

    def test_exception_after_durable_commit_never_restarts_old_code(self):
        real = migrate.migrate
        def committed_then_failed(*args, **kw):
            real(*args, **kw)
            raise OSError('interrupted after commit')
        with self.installation(), patch.object(migrate, 'migrate', side_effect=committed_then_failed):
            with self.assertRaisesRegex(OSError, 'after commit'):
                deploy.install()
        self.assertEqual(bot.Store(self.database).load()['version'], bot.VERSION)
        self.assertEqual(self.starts(), [])
        self.assertFalse(self.override.exists())

    def test_unit_failure_leaves_migrated_state_for_idempotent_repair(self):
        with self.installation(), patch.object(deploy, 'write_override', side_effect=OSError('unit failure')):
            with self.assertRaisesRegex(OSError, 'unit failure'):
                deploy.install()
        after = bot.Store(self.database).load()
        self.assertEqual(after['version'], bot.VERSION)
        self.assertEqual(self.starts(), [])
        with self.installation():
            deploy.install(repair=True)
        self.assertEqual(bot.Store(self.database).load(), after)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.ex.writes, 0)
        self.assertEqual(len(list((self.root/'backups').glob('*.sqlite3'))), 1)

    def test_source_failure_happens_before_service_stop(self):
        def uncommitted(*args, **kw):
            if args[:2] == ('git', 'diff'):
                raise subprocess.CalledProcessError(1, args)
            return self.command(*args, **kw)
        (self.root/'competition_v31').mkdir()
        (self.root/'competition_v31/controller.py').write_text('changed')
        with patch.object(deploy, 'PROJECT', self.root), patch.object(deploy, 'run', side_effect=uncommitted):
            with self.assertRaises(subprocess.CalledProcessError):
                deploy.gate(self.root)
        self.assertFalse(any('stop' in c for c in self.commands))

    def test_gate_requires_completed_cycle_and_allowed_window(self):
        (self.root/'competition_v31').mkdir()
        with patch.object(deploy, 'PROJECT', self.root), patch.object(deploy, 'run', side_effect=self.command):
            self.assertTrue(deploy.gate(self.root))
            self.ex.clock = MIDNIGHT + 10*60
            with self.assertRaisesRegex(RuntimeError, 'minutes 16-49'):
                deploy.gate(self.root)
            self.ex.clock = MIDNIGHT + 80*60
            with self.assertRaisesRegex(RuntimeError, 'minutes 16-49'):
                deploy.gate(self.root)

    def test_repair_only_accepts_migrated_state(self):
        (self.root/'competition_v31').mkdir()
        with patch.object(deploy, 'PROJECT', self.root), patch.object(deploy, 'run', side_effect=self.command):
            with self.assertRaisesRegex(RuntimeError, 'already migrated'):
                deploy.gate(self.root, repair=True)
            migrate.migrate(self.ex, bot.Store(self.database), self.root/'backups')
            self.assertTrue(deploy.gate(self.root, repair=True))
            with self.assertRaisesRegex(RuntimeError, 'repair-service'):
                deploy.gate(self.root)


class AdapterDeadlineTests(unittest.TestCase):
    def test_real_adapter_cannot_transmit_after_throttle_crosses_deadline(self):
        client = api.Client('fake-test-key', 'fake-test-secret')
        client.clock_checked = api.time.monotonic()
        clock = [100.0]
        def throttle():
            clock[0] = 102.0
        with patch.object(api.time, 'time', side_effect=lambda:clock[0]), \
                patch.object(client, 'throttle', side_effect=throttle), \
                patch('urllib.request.build_opener') as network:
            with self.assertRaises(api.NotSent):
                client.request('/v3/place_order', {'pair':'BTC/USD'}, method='POST', signed=True, deadline=101)
            network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
