"""Discover exchange-wide symbols and their first public Binance hourly candle."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime,timezone
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parent


def get(url):
    with urlopen(url,timeout=20) as response:return json.load(response)


def main():
    evidence=ROOT/'evidence';evidence.mkdir(exist_ok=True)
    r=get('https://mock-api.roostoo.com/v3/exchangeInfo')
    b=get('https://data-api.binance.vision/api/v3/exchangeInfo')
    (evidence/'roostoo_exchange.json').write_text(json.dumps(r,indent=2)+'\n')
    (evidence/'binance_exchange.json').write_text(json.dumps(b,indent=2)+'\n')
    symbols={x['symbol']:x for x in b['symbols']};rows={}
    old=json.loads((evidence/'catalog.json').read_text())['assets'] if (evidence/'catalog.json').exists() else {}
    def check(item):
        pair,rule=item;asset=rule.get('Coin');symbol=str(asset)+'USDT';market=symbols.get(symbol,{})
        mapped=rule.get('Unit')=='USD' and market.get('baseAsset')==asset and market.get('quoteAsset')=='USDT'
        trading=mapped and rule.get('CanTrade') is True and market.get('status')=='TRADING'
        row=dict(pair=pair,symbol=symbol,asset_type=rule.get('AssetType'),mapped=mapped,currently_tradable=trading,
            candidate=trading and rule.get('AssetType')=='crypto',roostoo_rules=rule,binance_status=market.get('status'))
        if mapped:
            try:
                first=old.get(asset,{}).get('first_open_ms')
                if first is None:
                    candles=get('https://data-api.binance.vision/api/v3/klines?'+urlencode(dict(symbol=symbol,interval='1h',startTime=0,limit=1)))
                    first=int(candles[0][0])
                row.update(first_open_ms=first,first_open_utc=datetime.fromtimestamp(first/1000,timezone.utc).isoformat())
            except Exception as exc:row['history_discovery_error']=type(exc).__name__+': '+str(exc)
        return asset,row
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(check,item) for item in r['TradePairs'].items()]
        for index,future in enumerate(as_completed(futures),1):
            asset,row=future.result();rows[asset]=row
            (evidence/'catalog.json').write_text(json.dumps(dict(generated_utc=datetime.now(timezone.utc).isoformat(),assets=rows),indent=2)+'\n')
            if index%20==0:print('DISCOVERED',index,'/',len(futures),flush=True)
    spec=json.loads((ROOT/'spec.json').read_text());base=spec['baseline']
    requested=spec.get('requested',spec['additional'][:10])
    candidate=[a for a,r in sorted(rows.items()) if r['candidate'] and a not in base]
    spec['requested']=requested
    spec['additional']=[a for a in requested if a in candidate]+[a for a in candidate if a not in requested]
    spec['collect_assets']=sorted(a for a,r in rows.items() if r['mapped'])
    spec['excluded_from_trading']={a:('ASSET_CLASS_REVIEW' if r['asset_type']!='crypto' else 'MARKET_UNAVAILABLE') for a,r in rows.items() if not r['candidate']}
    spec['api_spacing_seconds']=3.1
    (ROOT/'spec.json').write_text(json.dumps(spec,indent=2)+'\n')
    print('CANDIDATE_CRYPTO',len(base)+len(spec['additional']),'COLLECT',len(spec['collect_assets']),'EXCLUDED',len(spec['excluded_from_trading']),flush=True)


if __name__=='__main__':main()
