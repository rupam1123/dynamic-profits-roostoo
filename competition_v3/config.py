"""Release policy validation; reject nonfinite numbers, unknown fields and unsafe caps."""
from decimal import Decimal as D
from datetime import datetime

def validate(c,now):
    keys={'position_fraction','gross_fraction','max_positions','daily_vol_target','min_quote_volume_24h','max_spread',
          'max_correlation','end_utc','universe','ladder','filters','guard','sleeves','risk','rotation','execution'}
    if set(c)!=keys:raise ValueError('Unknown or missing policy fields')
    def num(x,lo,hi):
        v=D(str(x))
        if not v.is_finite() or not D(str(lo))<=v<=D(str(hi)):raise ValueError('Policy number outside release bounds')
        return v
    def integer(x,lo,hi):
        if type(x) is not int or not lo<=x<=hi:raise ValueError('Invalid policy integer')
    def fields(x,want):
        if not isinstance(x,dict) or set(x)!=set(want.split()):raise ValueError('Unexpected nested policy fields')
    num(c['position_fraction'],.001,.15);num(c['gross_fraction'],.01,.55);integer(c['max_positions'],1,5)
    num(c['daily_vol_target'],.001,.03);num(c['min_quote_volume_24h'],1000000,1e12)
    num(c['max_spread'],.00001,.005);num(c['max_correlation'],.1,.95)
    if c['universe']!=['BTC','ETH','SOL','BNB','XRP','ADA','DOGE','AVAX','LINK','DOT','LTC','NEAR']:raise ValueError('Release universe changed')
    x=c['ladder'];fields(x,'soft hard pause_hours recover_hours floor_fraction')
    if num(x['soft'],.001,.03)>=num(x['hard'],.005,.05):raise ValueError('Invalid ladder order')
    integer(x['pause_hours'],12,72);integer(x['recover_hours'],24,168);num(x['floor_fraction'],.80,.99)
    x=c['filters'];fields(x,'z_max r6_max btc_m24_off btc_vol_ratio')
    num(x['z_max'],1,3);num(x['r6_max'],.005,.05);num(x['btc_m24_off'],-.10,-.01);num(x['btc_vol_ratio'],1.1,4)
    x=c['guard'];fields(x,'local_hour utc_offset_hours mode')
    integer(x['local_hour'],0,23)
    if x['utc_offset_hours']!=8 or x['mode']!='MONITOR_ONLY':raise ValueError('Activity cannot force trades')
    x=c['risk'];fields(x,'per_position portfolio core_gross cooldown_hours stop_fraction atr_exits atr_stop min_stop max_stop partial_at_r trail_at_r trail_r')
    num(x['per_position'],.001,.005);num(x['portfolio'],.001,.025);num(x['core_gross'],.01,.55)
    integer(x['cooldown_hours'],6,72);num(x['stop_fraction'],.01,.08)
    if type(x['atr_exits']) is not bool:raise ValueError('ATR switch must be boolean')
    num(x['atr_stop'],1,5);num(x['min_stop'],.005,.05);num(x['max_stop'],.01,.08)
    if D(x['min_stop'])>D(x['max_stop']):raise ValueError('Stop distances reversed')
    for k in ('partial_at_r','trail_at_r','trail_r'):num(x[k],.5,5)
    if D(x['trail_at_r'])<D(x['partial_at_r']):raise ValueError('Trail must start after partial threshold')
    names=set()
    if not isinstance(c['sleeves'],list) or len(c['sleeves'])>2:raise ValueError('Too many sleeves')
    for s in c['sleeves']:
        fields(s,'name family lookback rebalance_hours gross k')
        if s['name'] in names or s['name'] not in ('xs','ts') or s['family']!=s['name']:raise ValueError('Invalid sleeve')
        names.add(s['name']);num(s['gross'],.001,.15);integer(s['rebalance_hours'],24,336)
        if s['lookback'] not in (336,720):raise ValueError('Unsupported lookback')
        integer(s['k'],1,3) if s['name']=='xs' else integer(s['k'],0,0)
    x=c['rotation'];fields(x,'enabled min_hold_hours interval_hours min_score_ratio min_score_gap cost_multiple max_per_day')
    if type(x['enabled']) is not bool:raise ValueError('Rotation switch must be boolean')
    integer(x['min_hold_hours'],12,168);integer(x['interval_hours'],6,168);integer(x['max_per_day'],1,2)
    num(x['min_score_ratio'],1.2,5);num(x['min_score_gap'],.1,5);num(x['cost_multiple'],1,10)
    x=c['execution'];fields(x,'entry_type fee slippage cost_multiple limit_timeout_seconds max_writes_per_hour')
    if x['entry_type'] not in ('MARKET','LIMIT_TEST_ONLY'):raise ValueError('Unsupported execution mode')
    num(x['fee'],.001,.01);num(x['slippage'],.0005,.01);num(x['cost_multiple'],1,5)
    integer(x['limit_timeout_seconds'],15,120);integer(x['max_writes_per_hour'],1,8)
    epoch=None
    if c['end_utc'] is not None:
        t=datetime.fromisoformat(c['end_utc'].replace('Z','+00:00'))
        if t.tzinfo is None:raise ValueError('Deadline needs timezone')
        epoch=t.timestamp()
    # Past deadlines remain valid: exits must still run.
    return dict(c,end_epoch=epoch)
