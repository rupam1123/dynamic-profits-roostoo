"""Pure admission checks for proposed assets; held-asset signals remain available for exits."""
from decimal import Decimal
import math


def eligible(signal, minimum_hours=721, minimum_volume='5000000'):
    if signal is None or signal.get('candles',0)<minimum_hours:
        return False, 'INSUFFICIENT_CONTIGUOUS_HISTORY'
    try:
        volume=Decimal(signal['quote_volume_24h'])
        if not volume.is_finite() or volume<Decimal(minimum_volume):return False,'LOW_QUOTE_VOLUME'
        for key in ('close','daily_vol','momentum_720h','score'):
            if not math.isfinite(float(signal[key])):return False,'NONFINITE_FEATURE'
        if float(signal['close'])<=0 or float(signal['daily_vol'])<=0:return False,'INVALID_PRICE_OR_VOLATILITY'
    except (KeyError,TypeError,ValueError,ArithmeticError):return False,'INVALID_FEATURE'
    return True,'ELIGIBLE'


def filtered_signals(signals, baseline_pairs, held_pairs):
    return {p:s for p,s in signals.items() if p in baseline_pairs or p in held_pairs or eligible(s)[0]}


def target_pairs(signals, baseline_pairs, extras):
    # The legacy portfolio still needs its original histories. A missing new listing
    # cannot veto every other asset's allocation.
    return list(baseline_pairs)+[p for p in extras if eligible(signals.get(p))[0]]
