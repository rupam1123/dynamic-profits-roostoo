"""Research simulator for Dynamic Profits V2 and candidate upgrades.

Decision at hour index i uses candles [0, i) (all closed); market fills at open[i]
with slippage, fee 0.1%/side (market orders). Intrabar stops (when enabled) use
bar i's high/low, stop checked before profit target (conservative ordering).
Baseline mode mirrors competition_v2.replay + strategy.candidates conventions.
"""
import csv, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ASSETS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC", "NEAR"]
FEE = 0.001

BASE = dict(
    assets=ASSETS, slip=0.0005,
    position_fraction=0.10, gross_fraction=0.30, max_positions=3, daily_vol_target=0.02,
    min_qv=5e6, max_corr=0.85, cooldown=12,
    latch=True,            # V2 permanent $3k drawdown stop
    ladder=False,          # replace latch with de-risk ladder + pause
    guard=False,           # daily activity guard (GMT+8 days)
    atr_exits=False,       # ATR stop / breakeven / partial / trail / time / m72 exit
    atr_k=2.5, be_k=1.5, tp_k=2.0, time_stop_h=72,
    stop_on=True, be_on=True, tp_on=True, time_on=True, exit_sig=None,  # exit_sig None -> m72 if atr_exits else m168
    mode="trend",  # trend | breakout | meanrev
    mr_z=2.5, mr_hold=12, mr_stop=0.03, bo_n=72, bo_exit=24,
    sleeves=(), core_count_own=False, ladder_soft=0.015, ladder_hard=0.03,   # tuple of (name, family, L, reb, gross) in priority order; family ts|xs
    guard_steps=("close", "entry", "trim", "probe"),
    probe_frac=0.02, guard_entry_mult=0.5, guard_hour=20,
    entry_filters=False,   # z24 / r6 overextension + BTC regime
    z_max=2.0, r6_max=0.03,
    risk_sizing=False,     # risk-per-trade sizing with bigger caps
    risk_per_trade=0.005, pos_cap=0.15, gross_cap=0.55, max_pos_risk=5,
)


def load(folder, assets=ASSETS):
    data = {}
    stamps = None
    for a in assets:
        path = next(Path(folder).glob(f"{a}USDT_1h_*.csv"))
        rows = list(csv.DictReader(open(path)))
        ts = np.array([datetime.fromisoformat(r["open_time_utc"]).timestamp() for r in rows], dtype=np.int64)
        arr = {k: np.array([float(r[k]) for r in rows]) for k in ("open", "high", "low", "close", "volume")}
        if stamps is None: stamps = ts
        assert np.array_equal(ts, stamps), f"{a} not aligned"
        data[a] = arr
    return stamps, data


def _roll_mean(x, w):
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full(len(x), np.nan)
    out[w - 1:] = (c[w:] - c[:-w]) / w
    return out


def _roll_std(x, w):
    m = _roll_mean(x, w); m2 = _roll_mean(x * x, w)
    return np.sqrt(np.maximum(m2 - m * m, 0))


def features(stamps, data):
    """f[a][key][i] = value known at decision index i (uses candles < i)."""
    F = {}
    for a, d in data.items():
        o, h, l, c, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
        n = len(c)
        lr = np.zeros(n); lr[1:] = np.log(c[1:] / c[:-1])
        vol_end = _roll_std(lr, 72) * math.sqrt(24)          # through bar j
        prevc = np.roll(c, 1); prevc[0] = c[0]
        tr = np.maximum(h - l, np.maximum(abs(h - prevc), abs(l - prevc)))
        atr_end = _roll_mean(tr, 14) / c
        qv_end = _roll_mean(c * v, 24) * 24
        sma24 = _roll_mean(c, 24); sd24 = _roll_std(c, 24)
        z_end = (c - sma24) / np.where(sd24 > 0, sd24, np.nan)
        def lag(x):  # value at decision i = value at end of bar i-1
            y = np.full(n, np.nan); y[1:] = x[:-1]; return y
        def mom(k):
            y = np.full(n, np.nan); y[k + 1:] = c[k:-1] / c[:-k - 1] - 1; return y
        F[a] = dict(close=lag(c), m24=mom(24), m72=mom(72), m168=mom(168), m336=mom(336), m720=mom(720), r6=mom(6),
                    vol=lag(vol_end), atr=lag(atr_end), qv=lag(qv_end), z24=lag(z_end), lr=lr)
        m24, m72, m168 = F[a]["m24"], F[a]["m72"], F[a]["m168"]
        F[a]["dir"] = np.where((m168 > .01) & (m24 > 0) & (m72 > 0), 1,
                               np.where((m168 < -.01) & (m24 < 0) & (m72 < 0), -1, 0))
        def prior_ext(n, fn):
            y = np.full(n_, np.nan)
            for i in range(n + 1, n_):
                y[i] = fn(c[i - 1 - n:i - 1])
            return y
        n_ = n
        F[a]["hh72"] = prior_ext(72, np.max); F[a]["ll72"] = prior_ext(72, np.min)
        F[a]["hh24"] = prior_ext(24, np.max); F[a]["ll24"] = prior_ext(24, np.min)
        cl = F[a]["close"]
        F[a]["dir_breakout"] = np.where((cl > F[a]["hh72"]) & (m168 > 0), 1, np.where((cl < F[a]["ll72"]) & (m168 < 0), -1, 0))
        z = F[a]["z24"]
        F[a]["dir_meanrev"] = np.where((z < -2.5) & (m168 > 0), 1, np.where((z > 2.5) & (m168 < 0), -1, 0))
        F[a]["score"] = abs(.2 * m24 + .3 * m72 + .5 * m168) / np.maximum(F[a]["vol"], .005)
    b = F["BTC"]
    med = np.full(len(stamps), np.nan)
    for i in range(720, len(stamps)):
        med[i] = np.median(b["vol"][i - 720:i:6])
    F["_risk_off"] = (b["m24"] < -0.03) | (b["vol"] > 2 * med)
    return F


def corr(F, a, b, i):
    x = F[a]["lr"][i - 72:i]; y = F[b]["lr"][i - 72:i]
    if len(x) < 24: return 1.0
    sx, sy = x.std(), y.std()
    if sx == 0 or sy == 0: return 1.0
    return float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy))


class Sim:
    def __init__(self, stamps, data, F, cfg):
        self.s, self.d, self.F, self.c = stamps, data, F, cfg

    # ---------- accounting ----------
    def mark(self, i, px_key="open"):
        slip = self.c["slip"]; eq = self.cash
        for a, p in self.pos.items():
            px = self.d[a][px_key][i]
            if p["dir"] == 1: eq += p["q"] * px * (1 - slip) * (1 - FEE)
            else:
                pe = px * (1 + slip)
                eq += p["col"] + max(p["q"] * (p["entry"] - pe), -p["col"]) - p["q"] * pe * FEE
        return eq

    def gross(self, i, book="core"):
        g = 0.0
        for a, p in self.pos.items():
            if p.get("book", "core") != book: continue
            m = p["q"] * self.d[a]["open"][i]
            g += max(m, p["col"]) if p["dir"] == -1 else m
        return g

    def fill_day(self, i):
        return (int(self.s[i]) + 8 * 3600) // 86400   # GMT+8 day

    def record(self, i, why, pair=None):
        self.log.append((i, pair, why))
        self.fills += 1; self.days.add(self.fill_day(i)); self.why[why] = self.why.get(why, 0) + 1

    def close(self, a, i, frac=1.0, price=None, why="exit"):
        p = self.pos[a]; slip = self.c["slip"]
        raw = self.d[a]["open"][i] if price is None else price
        q = p["q"] * frac; col = p["col"] * frac
        if p["dir"] == 1:
            px = raw * (1 - slip); fee = q * px * FEE; self.cash += q * px - fee
        else:
            px = raw * (1 + slip); fee = q * px * FEE
            self.cash += col + max(q * (p["entry"] - px), -col) - fee
        self.fees += fee; self.record(i, why, a)
        if frac >= 0.999:
            del self.pos[a]; self.last_exit[a] = i
        else:
            p["q"] -= q; p["col"] -= col

    def open(self, a, i, direction, budget, why="entry", probe_h=None, book="core"):
        if a in self.pos: return False
        slip = self.c["slip"]; o = self.d[a]["open"][i]
        if direction == 1:
            px = o * (1 + slip); q = budget / (px * 1.01101); col = 0.0
            fee = q * px * FEE; cost = q * px + fee
        else:
            px = o * (1 - slip); q = budget / px; col = budget
            fee = q * px * FEE; cost = col + fee
        if cost > self.cash or q <= 0: return False
        self.cash -= cost; self.fees += fee; self.record(i, why, a)
        atr = self.F[a]["atr"][i]
        k = self.c["atr_k"]
        self.pos[a] = dict(dir=direction, q=q, entry=px, col=col, atr=atr, i0=i, partial=False,
                           hw=px, stop=px * (1 - direction * k * atr), probe_h=probe_h, book=book)
        return True

    # ---------- policy ----------
    def risk_mult(self, i, mark):
        c = self.c
        if not c["ladder"]: return 1.0
        dd = 1 - mark / self.peak
        m = 1.0 if dd < c["ladder_soft"] else .5
        if i < self.half_until: m = min(m, .5)
        if mark < 0.94 * self.initial: m = .25
        return m

    def candidates(self, i, mult):
        c, F = self.c, self.F
        if self.stopped: return []
        eq = min(self.initial, self.last_eq)
        gross_frac = c["gross_cap"] if c["risk_sizing"] else c["gross_fraction"]
        maxpos = c["max_pos_risk"] if c["risk_sizing"] else c["max_positions"]
        room = max(0.0, eq * gross_frac - self.gross(i))
        cash = max(0.0, self.cash - 1)
        names = [a for a in c["assets"] if not math.isnan(F[a]["score"][i])]
        ranked = sorted(names, key=lambda a: (-F[a]["score"][i], a))
        occupied = {a: p["dir"] for a, p in self.pos.items()}
        n_core = sum(1 for p in self.pos.values() if p.get("book", "core") == "core")
        out = []
        risk_off = bool(F["_risk_off"][i]) if c["entry_filters"] else False
        for a in ranked:
            if a in occupied: continue
            key = {"trend": "dir", "breakout": "dir_breakout", "meanrev": "dir_meanrev"}[c["mode"]]
            if c["mode"] == "meanrev":
                zz = F[a]["z24"][i]; m168 = F[a]["m168"][i]
                d = 1 if (zz < -c["mr_z"] and m168 > 0) else -1 if (zz > c["mr_z"] and m168 < 0) else 0
            else:
                d = int(F[a][key][i])
            if not d: continue
            if i - self.last_exit.get(a, -10**9) < c["cooldown"]: continue
            if (n_core if c["core_count_own"] else len(occupied)) >= maxpos: continue
            if F[a]["qv"][i] < c["min_qv"]: continue
            if c["entry_filters"]:
                z, r6 = F[a]["z24"][i], F[a]["r6"][i]
                if d == 1 and (z > c["z_max"] or r6 > c["r6_max"]): continue
                if d == -1 and (z < -c["z_max"] or r6 < -c["r6_max"]): continue
                if d == 1 and risk_off and a != "BTC": continue
            if any(corr(F, a, h, i) * d * hd > c["max_corr"] for h, hd in occupied.items()): continue
            vol = max(F[a]["vol"][i], .005)
            if c["risk_sizing"]:
                stop_dist = min(max(c["atr_k"] * F[a]["atr"][i], .015), .06)
                r = eq * c["risk_per_trade"] * mult * (.5 if risk_off else 1)
                want = min(r / stop_dist, eq * c["pos_cap"])
            else:
                want = eq * c["position_fraction"] * min(1, c["daily_vol_target"] / vol) * mult
            budget = math.floor(min(want, room / 1.02, cash / 1.02) * 100) / 100
            if budget < 25: continue
            out.append((a, d, budget)); occupied[a] = d; n_core += 1
            room -= budget * 1.02; cash -= budget * 1.02
        return out

    def exit_signal(self, a, i):
        c, F, p = self.c, self.F, self.pos[a]
        d = p["dir"]; close = F[a]["close"][i]
        if d * (close / p["entry"] - 1) <= -0.08: return "backstop"
        if p["probe_h"] is not None and i - p["i0"] >= p["probe_h"]: return "probe_time"
        if c["mode"] == "meanrev":
            if d * F[a]["z24"][i] >= 0: return "mr_target"
            if i - p["i0"] >= c["mr_hold"]: return "mr_time"
            if d * (close / p["entry"] - 1) <= -c["mr_stop"]: return "mr_stop"
            return None
        if c["mode"] == "breakout":
            if (d == 1 and close < F[a]["ll24"][i]) or (d == -1 and close > F[a]["hh24"][i]): return "bo_exit"
            return None
        sig = c["exit_sig"] or ("m72" if c["atr_exits"] else "m168")
        if c["atr_exits"] and c["time_on"] and i - p["i0"] >= c["time_stop_h"] and d * (close / p["entry"] - 1) < 0.005:
            return "time_stop"
        if d * F[a][sig][i] <= 0: return sig + "_flip"
        return None

    def intrabar(self, i):
        """ATR stop / breakeven / partial profit inside bar i."""
        c = self.c; slip_k = c["atr_k"]
        for a in list(self.pos):
            p = self.pos[a]; d = p["dir"]
            h, l, o = self.d[a]["high"][i], self.d[a]["low"][i], self.d[a]["open"][i]
            adverse = l if d == 1 else h
            if c["stop_on"] and d * (adverse - p["stop"]) <= 0:
                px = min(o, p["stop"]) if d == 1 else max(o, p["stop"])
                self.close(a, i, price=px, why="atr_stop"); continue
            fav = h if d == 1 else l
            dist = p["atr"] * p["entry"]
            if c["tp_on"] and not p["partial"] and d * (fav - p["entry"]) >= c["tp_k"] * dist:
                tgt = p["entry"] + d * c["tp_k"] * dist
                self.close(a, i, frac=0.5, price=tgt, why="partial_tp"); p["partial"] = True
            if c["be_on"] and d * (fav - p["entry"]) >= c["be_k"] * dist:
                be = p["entry"] * (1 + d * 0.002)
                p["stop"] = max(p["stop"], be) if d == 1 else min(p["stop"], be)

    def guard(self, i, mult):
        """Daily activity guard: GMT+8 >= 20:00 with no fill today."""
        F = self.F
        hour_local = ((int(self.s[i]) + 8 * 3600) // 3600) % 24
        if hour_local < self.c["guard_hour"] or self.fill_day(i) in self.days: return
        steps = self.c["guard_steps"]
        if "close" in steps:
            for a, p in list(self.pos.items()):
                if p["dir"] * F[a]["m24"][i] < 0:
                    self.close(a, i, why="guard_close"); return
        if "entry" in steps and not self.stopped and not self.paused(i):
            picks = self.candidates(i, mult * self.c["guard_entry_mult"])
            if picks:
                a, d, b = picks[0]
                if self.open(a, i, d, b, why="guard_entry"): return
        longs = [a for a, p in self.pos.items() if p["dir"] == 1]
        if "trim" in steps and longs:
            weakest = min(longs, key=lambda a: F[a]["score"][i])
            self.close(weakest, i, frac=0.25, why="guard_trim"); return
        if not self.stopped and "probe" in steps:
            names = [a for a in self.c["assets"] if a not in self.pos and self.last_exit.get(a) != i
                     and not math.isnan(F[a]["score"][i]) and F[a]["m24"][i] != 0]
            if names:
                a = max(names, key=lambda x: F[x]["score"][i])
                d = 1 if F[a]["m24"][i] > 0 else -1
                if self.open(a, i, d, round(min(self.initial, self.last_eq) * self.c["probe_frac"], 2), why="guard_probe", probe_h=24): return
        shorts = [a for a, p in self.pos.items() if p["dir"] == -1]
        if "trim" in steps and shorts:
            smallest = min(shorts, key=lambda a: self.pos[a]["col"])
            self.close(smallest, i, why="guard_close_small_short")

    def rebalance(self, i, mult, name, family, L, reb, gross):
        F = self.F; key = "m%d" % L
        if not ((int(self.s[i]) // 3600) % reb and name in self.sleeve_seen):
            moms = {a: F[a][key][i] for a in self.c["assets"]}
            if any(math.isnan(v) for v in moms.values()): return
            self.sleeve_seen.add(name)
            self.sleeve_target[name] = self.sleeve_compute(i, mult, family, moms, gross)
        if name not in self.sleeve_target: return
        target = self.sleeve_target[name]
        for a in list(self.pos):
            p = self.pos[a]
            if p.get("book") != name: continue
            if a not in target or target[a][0] != p["dir"]:
                self.close(a, i, why=name + "_rebal")
        if self.stopped or self.paused(i): return
        for a, (d, notional) in target.items():
            if a in self.pos or self.last_exit.get(a) == i: continue
            if notional < 25 or notional * 1.02 > self.cash - 1: continue
            self.open(a, i, d, notional, why=name + "_open", book=name)

    def sleeve_compute(self, i, mult, family, moms, gross):
        F = self.F
        score = {a: moms[a] / max(F[a]["vol"][i], .005) for a in moms}
        target = {}
        if family == "xs":
            order = sorted(score, key=lambda a: score[a])
            for a in order[-2:]: target[a] = 1.0
            for a in order[:2]: target[a] = -1.0
        else:
            for a in moms:
                if moms[a] != 0: target[a] = math.copysign(1 / max(F[a]["vol"][i], .005), moms[a])
        eq = min(self.initial, self.last_eq)
        tot = sum(abs(v) for v in target.values())
        if tot <= 0: return {}
        return {a: (1 if v > 0 else -1, math.floor(eq * gross * mult * abs(v) / tot * 100) / 100) for a, v in target.items()}

    def paused(self, i):
        return i < self.paused_until

    # ---------- run ----------
    def run(self, idx):
        c = self.c
        self.initial = 100000.0; self.cash = 100000.0; self.last_eq = self.cash
        self.log = []; self.marks = []
        self.pos = {}; self.last_exit = {}; self.fills = 0; self.fees = 0.0; self.days = set(); self.why = {}
        self.peak = self.cash; self.stopped = False; self.paused_until = -1; self.half_until = -1
        self.stop_reason = None; self.sleeve_seen = set(); self.sleeve_target = {}
        curve = []; maxdd = 0.0; peak_curve = self.cash
        for i in idx:
            mark = self.mark(i); self.marks.append(mark)
            if c["ladder"] and self.paused_until == i:      # pause just ended
                self.peak = mark; self.half_until = i + 48
            self.peak = max(self.peak, mark)
            peak_curve = max(peak_curve, mark); maxdd = max(maxdd, 1 - mark / peak_curve)
            if c["latch"] and not c["ladder"] and self.peak - mark >= 3000 and not self.stopped:
                self.stopped = True; self.stop_reason = "LATCH"
            if c["ladder"] and not self.paused(i) and 1 - mark / self.peak >= c["ladder_hard"]:
                for a in list(self.pos): self.close(a, i, why="ladder_flat")
                self.paused_until = i + 24; self.stop_reason = "LADDER_PAUSE"
            mult = self.risk_mult(i, mark)
            # exits first
            for a in list(self.pos):
                if self.stopped: self.close(a, i, why="latch_flat"); continue
                if self.pos[a].get("book", "core") != "core": continue
                if c["atr_exits"]:
                    p = self.pos[a]; cl = self.F[a]["close"][i]
                    if p["dir"] == 1 and cl > p["hw"]: p["hw"] = cl
                    if p["dir"] == -1 and cl < p["hw"]: p["hw"] = cl
                    trail = p["hw"] - p["dir"] * c["atr_k"] * p["atr"] * p["entry"]
                    p["stop"] = max(p["stop"], trail) if p["dir"] == 1 else min(p["stop"], trail)
                why = self.exit_signal(a, i)
                if why: self.close(a, i, why=why)
            self.last_eq = self.mark(i)
            for sl in c["sleeves"]: self.rebalance(i, mult, *sl)
            if not self.stopped and not self.paused(i):
                for a, d, b in self.candidates(i, mult):
                    if a in self.pos or self.last_exit.get(a) == i: continue
                    self.open(a, i, d, b)
            if c["guard"]: self.guard(i, mult)
            if c["atr_exits"]: self.intrabar(i)
            curve.append(self.mark(i, "close"))
            peak_curve = max(peak_curve, curve[-1]); maxdd = max(maxdd, 1 - curve[-1] / peak_curve)
        return dict(curve=np.array(curve), maxdd=maxdd, fills=self.fills, fees=self.fees,
                    days=len(self.days), why=self.why, stop=self.stop_reason)


def metrics(res, hours):
    eq = np.concatenate([[100000.0], res["curve"]])
    r = np.diff(eq) / eq[:-1]
    ret = eq[-1] / eq[0] - 1
    ann = math.sqrt(24 * 365)
    sd = r.std(); dn = np.sqrt(np.mean(np.minimum(r, 0) ** 2))
    sharpe = r.mean() / sd * ann if sd > 0 else 0.0
    sortino = r.mean() / dn * ann if dn > 0 else 0.0
    dd = max(res["maxdd"], 1e-4)
    calmar = (ret * 24 * 365 / hours) / dd
    calmar = max(min(calmar, 50), -50)     # cap: tiny drawdowns explode Calmar
    sortino = max(min(sortino, 50), -50)
    return dict(ret=ret * 100, maxdd=res["maxdd"] * 100, sharpe=sharpe, sortino=sortino, calmar=calmar,
                composite=0.4 * sortino + 0.3 * sharpe + 0.3 * calmar, fills=res["fills"],
                active_days=res["days"], stop=res["stop"])
