"""Compare actual V2.1 and final controllers, including every simulated write.

Run from the repository: python -m research_v31.compare_baseline --data history12
Requires NumPy only for research. No credentials, network or real orders.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'research_v3'))
import compare as replay


STARTS = [(y, m, 1) for y in (2025, 2026) for m in (3, 5, 7, 9)] + [
    (2025, 10, 1), (2025, 11, 1), (2025, 12, 1),
    (2026, 4, 1), (2026, 6, 1), (2026, 8, 1)]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def traced_run(module, window, stamps, data, prepared, slip):
    trace = []
    original = replay.Exchange

    class TracedExchange(original):
        def request(self, endpoint, params=None, **kw):
            result = super().request(endpoint, params, **kw)
            if endpoint in ('/v3/place_order', '/v6/short_open', '/v6/short_close'):
                trace.append(dict(utc_epoch=self.clock, endpoint=endpoint,
                    params=copy.deepcopy(params), response=copy.deepcopy(result)))
            return result

    with patch.object(replay, 'Exchange', TracedExchange):
        result = replay.run(module, window, stamps, data, prepared, slip)
    result['trade_trace_sha256'] = digest(trace)
    return result, trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('history12'))
    parser.add_argument('--output', type=Path, default=Path('validation/v31_parity.json'))
    parser.add_argument('--small', action='store_true')
    args = parser.parse_args()
    stamps, data, qv = replay.load(args.data)
    starts = STARTS[:1] if args.small else STARTS
    windows = []
    for start in starts:
        first = int(replay.np.searchsorted(stamps, replay.ts(*start) - 8 * 3600))
        windows.append(list(range(first, first + 14 * 24)))
    prepared, error = replay.prepare(stamps, data, qv, {i for w in windows for i in w})
    result = dict(method='Actual V2.1 and final V3.1 controllers, identical hourly history, 0.1% fee per side; 5/15bp adverse slippage per fill.',
        strategy='Frozen uploaded V2.1 policy and trade allocation; no parameter tuning.',
        limits=['Retrospective, previously examined windows; not an untouched holdout.',
            'No intrahour prices, order-book depth, latency, outages or partial fills in this replay.',
            'Fresh-start simulations; migrated holdings and failure recovery are tested separately.',
            'An exchange fill day does not establish organizer eligibility.',
            'Open ending positions are valued with estimated closing fees and adverse slippage.'],
        feature_max_absolute_error=error, comparisons=[], all_trade_traces_equal=True)
    for slip in (.0005, .0015):
        for window in windows:
            old, old_trace = traced_run('competition_v21.controller', window, stamps, data, prepared, slip)
            new, new_trace = traced_run('competition_v31.controller', window, stamps, data, prepared, slip)
            same = old_trace == new_trace
            row = dict(start=old['start'], slippage_bps=int(slip * 10000), baseline=old, final=new,
                identical_trade_trace=same, identical_metrics=old == new)
            if not same:
                differing = next((i for i, (a, b) in enumerate(zip(old_trace, new_trace)) if a != b), min(len(old_trace), len(new_trace)))
                row['first_difference'] = dict(index=differing,
                    baseline=old_trace[differing:differing+1], final=new_trace[differing:differing+1])
                result['all_trade_traces_equal'] = False
            result['comparisons'].append(row)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + '\n')
            print(f"{row['slippage_bps']}bp {old['start'][:10]} trace_equal={same} fills={new['fills']} return={new['return_pct']:.4f}%", flush=True)
    result['summary'] = {}
    for slip in (5, 15):
        rows = [r['final'] for r in result['comparisons'] if r['slippage_bps'] == slip]
        result['summary'][str(slip)+'bp'] = dict(windows=len(rows),
            mean_return_pct=sum(r['return_pct'] for r in rows)/len(rows),
            worst_drawdown_pct=max(r['max_drawdown_pct'] for r in rows),
            fills=sum(r['fills'] for r in rows), unresolved=sum(r['unresolved'] for r in rows))
    result['data_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(args.data.glob('*.csv'))}
    result['source_sha256'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        for folder in (ROOT/'competition_v31', ROOT/'research_v3/reference/competition_v21')
        for p in sorted(folder.iterdir()) if p.suffix in ('.py', '.json')}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    if not result['all_trade_traces_equal']:
        raise SystemExit('Trade parity differs: investigate the recorded differences; do not silently tune the baseline.')
    print('PASS: every simulated order, quantity, timestamp and fill matches the frozen baseline.', flush=True)


if __name__ == '__main__':
    main()
