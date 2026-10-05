import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import short_roundtrip as short
from test_roundtrip_checks import Exchange

ACK = {'Success': True, 'ID': 4742, 'Pair': 'BTC/USD', 'OrderType': 'MARKET',
       'EntryPrice': 85649.14, 'ShortQty': 0.00011, 'Collateral': 9.42,
       'OpenFee': 0.009421, 'Status': 'OPEN', 'CreateTimestamp': 1791226772122}


class ObservedExchange(Exchange):
    def __init__(self):
        super().__init__()
        self.usd = Decimal('49999.97')
        self.pos = []
        self.lose_close_response = False

    def short_positions(self):
        return {'Success': True, 'Positions': self.pos.copy()}

    def request(self, endpoint, params=None, **kwargs):
        if endpoint == '/v6/short_open':
            self.writes += 1
            self.usd -= Decimal('9.42') + Decimal('0.009421')
            self.pos = [ACK.copy()]
            return ACK.copy()
        if endpoint != '/v6/short_close':
            return super().request(endpoint, params, **kwargs)
        self.writes += 1
        qty = Decimal('0.00011')
        price = Decimal('85700')
        pnl = qty * (Decimal('85649.14') - price)
        fee = qty * price * Decimal('.001')
        returned = Decimal('9.42') + pnl - fee
        self.usd += returned
        self.pos = []
        if self.lose_close_response:
            raise TimeoutError('Close filled but response lost')
        return dict(Success=True, FullyClosed=True, ClosedQty=str(qty), ClosePrice=str(price),
                    CloseFee=str(fee), RealizedPNL=str(pnl), ReturnAmount=str(returned))


class RecoveryChecks(unittest.TestCase):
    def halted(self, client, path):
        # Reproduce the old validator's failure after the real-shaped OPEN acknowledgement.
        with patch.object(short, 'validate_size', side_effect=ValueError('old check')):
            with self.assertRaises(ValueError):
                short.run(client, path)
        self.assertEqual(client.writes, 1)

    def test_actual_response_completes_with_fixed_validation(self):
        with tempfile.TemporaryDirectory() as d, patch.object(short, 'emit'):
            c = ObservedExchange()
            short.run(c, Path(d) / 'test.db')
            self.assertEqual(c.writes, 2)
            self.assertEqual(c.pos, [])

    def test_recovery_closes_once_and_preserves_ack(self):
        with tempfile.TemporaryDirectory() as d, patch.object(short, 'emit'):
            path = Path(d) / 'test.db'
            c = ObservedExchange()
            self.halted(c, path)
            short.recover(c, 4742, path)
            self.assertEqual(c.writes, 2)
            self.assertEqual(c.pos, [])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT status FROM run').fetchone()[0], 'COMPLETE')
                self.assertEqual(json.loads(db.execute("SELECT response FROM steps WHERE name='open'").fetchone()[0]), ACK)
            with self.assertRaises(ValueError):
                short.recover(c, 4742, path)
            self.assertEqual(c.writes, 2)

    def test_wrong_identity_or_changed_position_never_closes(self):
        for change in ('account', 'id', 'quantity', 'missing', 'extra'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d, patch.object(short, 'emit'):
                path = Path(d) / 'test.db'
                c = ObservedExchange()
                self.halted(c, path)
                expected_id = 4742
                if change == 'account': c.key = 'different-account'
                if change == 'id': expected_id = 999
                if change == 'quantity': c.pos[0]['ShortQty'] = '0.00012'
                if change == 'missing': c.pos = []
                if change == 'extra': c.pos.append(ACK.copy())
                with self.assertRaises(ValueError):
                    short.recover(c, expected_id, path)
                self.assertEqual(c.writes, 1)

    def test_lost_close_response_is_not_resubmitted(self):
        with tempfile.TemporaryDirectory() as d, patch.object(short, 'emit'):
            path = Path(d) / 'test.db'
            c = ObservedExchange()
            self.halted(c, path)
            c.lose_close_response = True
            with self.assertRaises(ValueError): short.recover(c, 4742, path)
            self.assertEqual(c.pos, [])
            with self.assertRaises(ValueError): short.recover(c, 4742, path)
            self.assertEqual(c.writes, 2)

    def test_size_budget_remains_enforced(self):
        for collateral, qty in (('10.01', '.00011'), ('9.00', '.00011'), ('9.42', '.00012')):
            with self.assertRaises(ValueError):
                short.validate_size(Decimal(collateral), Decimal(qty), Decimal('85649.14'), Decimal('.00001'))


if __name__ == '__main__': unittest.main()
