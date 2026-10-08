"""Exact crypto mapping only. Discovery is separate from liquidity/signal admission."""
import re


def discover(roostoo,binance):
    if roostoo.get('IsRunning') is not True:raise ValueError('Roostoo exchange is unavailable')
    symbols=binance.get('symbols')
    if not isinstance(symbols,list):raise ValueError('Missing Binance symbol metadata')
    exact={r['symbol']:r for r in symbols if isinstance(r,dict) and 'symbol' in r}
    eligible={};skipped={}
    for pair,rule in roostoo.get('TradePairs',{}).items():
        reason=None
        if not isinstance(rule,dict) or not re.fullmatch(r'[A-Z0-9]+/USD',pair):reason='INVALID_PAIR'
        elif rule.get('AssetType')!='crypto':reason='NOT_CRYPTO'
        elif rule.get('CanTrade') is not True:reason='NOT_TRADABLE'
        else:
            asset=pair.split('/')[0];symbol=asset+'USDT';r=exact.get(symbol,{})
            if r.get('baseAsset')!=asset or r.get('quoteAsset')!='USDT' or r.get('status')!='TRADING' or r.get('isSpotTradingAllowed',True) is not True:reason='NO_EXACT_ACTIVE_SPOT_MAPPING'
            else:eligible[pair]=symbol
        if reason:skipped[pair]=reason
    if not eligible or 'BTC/USD' not in eligible:raise ValueError('Crypto universe/BTC regime mapping unavailable')
    return eligible,skipped
