import unittest
from pathlib import Path
import time
import guardian as g
from unittest.mock import patch

class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.now=1_800_000_000.0
        self.info={'ActiveState':'active','SubState':'running','MainPID':'2323','ActiveEnterTimestampMonotonic':'10000'}
        self.events=[(self.now-15,'2323','',{'status':'V4_HEARTBEAT','quote_age_seconds':6,'dynamic_assets':65}),
                     (self.now-30,'2323','',{'status':'V4_DATA_READY'})]

    @patch.object(g,'service_age',return_value=500)
    def test_healthy(self, _):
        self.assertEqual(g.evaluate(self.info,self.events,self.now)[0], 'HEALTHY')

    @patch.object(g,'service_age',return_value=500)
    def test_stale_quotes(self, _):
        self.events[0][3]['quote_age_seconds']=140
        self.assertEqual(g.evaluate(self.info,self.events,self.now)[0], 'STALE_QUOTES')

    @patch.object(g,'service_age',return_value=500)
    def test_stale_data(self, _):
        self.events[1]=(self.now-400,'2323','',{'status':'V4_DATA_READY'})
        self.assertEqual(g.evaluate(self.info,self.events,self.now)[0], 'STALE_DATA')

    @patch.object(g,'service_age',return_value=500)
    def test_process_id_prevents_stale_healthy_signals(self, _):
        self.info['MainPID']='2324'
        self.assertEqual(g.evaluate(self.info,self.events,self.now)[0], 'STALE_HEARTBEAT')

    def test_manual_stop_respected(self):
        self.info['ActiveState']='inactive'
        self.assertEqual(g.evaluate(self.info,self.events,self.now)[0], 'MANUALLY_STOPPED')

    @patch.object(g,'service_age',return_value=15)
    def test_startup_grace(self, _):
        self.assertEqual(g.evaluate(self.info,[],self.now)[0], 'STARTING')

    def test_service_failed(self):
        self.info['ActiveState']='failed'
        self.assertEqual(g.evaluate(self.info,[],self.now)[0], 'SERVICE_DOWN')

if __name__ == '__main__':
    unittest.main()

class GuardianRecoveryTests(unittest.TestCase):
    def test_unresolved_attempt_prevents_restarting(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            with patch.object(g,'ROOT',folder),patch.object(g,'STATE',folder/'state.json'),patch.object(g,'ALERTS',folder/'alerts.jsonl'), \
                 patch.object(g,'service_info',return_value={'ActiveState':'active','SubState':'running','MainPID':'44'}), \
                 patch.object(g,'read_events',return_value=[]), \
                 patch.object(g,'evaluate',return_value=('STALE_QUOTES','quotes stalled',True)), \
                 patch.object(g,'unresolved_count',return_value=1),patch.object(g,'command') as cmd:
                for _ in range(4):g.run('--apply')
                self.assertNotIn('restart_times',g.load_state())
                cmd.assert_not_called()

    def test_safe_restart_is_rate_limited(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            with patch.object(g,'ROOT',folder),patch.object(g,'STATE',folder/'state.json'),patch.object(g,'ALERTS',folder/'alerts.jsonl'), \
                 patch.object(g,'service_info',return_value={'ActiveState':'active','SubState':'running','MainPID':'44'}), \
                 patch.object(g,'read_events',return_value=[]), \
                 patch.object(g,'evaluate',return_value=('STALE_QUOTES','quotes stalled',True)), \
                 patch.object(g,'unresolved_count',return_value=0),patch.object(g,'command') as cmd:
                for _ in range(7):g.run('--apply')
                self.assertEqual(cmd.call_count,2)
                self.assertEqual(cmd.call_args_list[0].args[:2], ('systemctl','stop'))
                self.assertEqual(cmd.call_args_list[1].args[:2], ('systemctl','start'))
                self.assertEqual(len(g.load_state()['restart_times']),1)

    def test_appearing_pending_attempt_after_stop_blocks_start(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            with patch.object(g,'ROOT',folder),patch.object(g,'STATE',folder/'state.json'),patch.object(g,'ALERTS',folder/'alerts.jsonl'), \
                 patch.object(g,'service_info',return_value={'ActiveState':'active','SubState':'running','MainPID':'44'}), \
                 patch.object(g,'read_events',return_value=[]), \
                 patch.object(g,'evaluate',return_value=('STALE_QUOTES','quotes stalled',True)), \
                 patch.object(g,'unresolved_count',side_effect=[0,1]),patch.object(g,'command') as cmd:
                for _ in range(4):g.run('--apply')
                self.assertEqual(cmd.call_count,1)
                self.assertEqual(cmd.call_args_list[0].args[:2],('systemctl','stop'))
                self.assertTrue(any('RESTART_BLOCKED' in line for line in (folder/'alerts.jsonl').read_text().splitlines()))
