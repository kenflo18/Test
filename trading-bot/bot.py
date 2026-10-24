#!/usr/bin/env python3
"""Aggressiver Mean-Reversion-Bot (Paper-Trading) fuer Krypto, Long + Short, 1h-Kerzen.
Kauft Ueberverkauft-Extreme (unter Bollinger-Band 2,5 SD + RSI<30), shortet Ueberkauft-Extreme.

Daten: oeffentliche Binance-Marktdaten (kein API-Key noetig).
Modi:
  python bot.py backtest --days 14   -> Strategie auf den letzten Tagen testen
  python bot.py live --hours 72      -> 72 h Paper-Trading mit 500 $ (setzt nach Neustart fort)
"""
import argparse, csv, json, math, os, sys, time, urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("BOT_API", "https://data-api.binance.vision")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
TRADES = os.path.join(HERE, "trades.csv")
STATUS = os.path.join(HERE, "status.md")
LOG = os.path.join(HERE, "bot.log")

P = dict(
    start_equity=500.0,
    interval="1h", bar_ms=3_600_000,
    bb_n=20, bb_k=2.5, rsi_lo=30,
    atr_n=14,
    risk=0.03,          # 3 % Risiko pro Trade (aggressiv)
    stop_atr=2.0, max_stop_pct=0.05,   # Stop: 2 ATR, hoechstens 5 % vom Einstieg
    trail_trigger_atr=1.0, trail_atr=1.0,  # ab +1 ATR Gewinn eng nachziehen
    time_stop_bars=12,  # nach 12 h ohne Fortschritt raus
    leverage=3.0,       # max. Gesamtexposure = 3x Kapital
    max_pos=2,
    fee=0.0005, slip=0.0003,  # Futures-Taker-Gebuehr + Slippage
    pause_dd=0.12,      # -12 % vom Hoch -> 6 h Pause
    pause_hours=6,
    kill_dd=0.25,       # -25 % vom Start -> Bot stoppt endgueltig
    cooldown_bars=0,
)

def log(msg):
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

def get(path, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(BASE + path, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == tries - 1: raise
            time.sleep(2 ** i)

def klines(sym, interval, limit=500, end=None):
    q = f"/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}"
    if end: q += f"&endTime={end}"
    return [dict(t=k[0], o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]), v=float(k[5]), ct=k[6]) for k in get(q)]

# ---------- Indikatoren ----------
def ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k); out.append(e)
    return out

def rsi(xs, n=14):
    out, ag, al = [50.0] * len(xs), 0.0, 0.0
    for i in range(1, len(xs)):
        d = xs[i] - xs[i - 1]; g, l = max(d, 0), max(-d, 0)
        if i <= n: ag += g / n; al += l / n
        else: ag = (ag * (n - 1) + g) / n; al = (al * (n - 1) + l) / n
        if i >= n: out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out

def atr(bars, n):
    out, a = [], None
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i-1]["c"]), abs(b["l"] - bars[i-1]["c"]))
        a = tr if a is None else (a * (n - 1) + tr) / n; out.append(a)
    return out

def signal(bars):
    """Signal auf dem letzten abgeschlossenen Bar: ('long'|'short'|None, atr)."""
    n = P["bb_n"]
    if len(bars) < 60: return None, 0
    c = [b["c"] for b in bars]; w = c[-n:]
    m = sum(w) / n; sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    r = rsi(c)[-1]; a = atr(bars, P["atr_n"])[-1]
    if c[-1] < m - P["bb_k"] * sd and r < P["rsi_lo"]: return "long", a
    if c[-1] > m + P["bb_k"] * sd and r > 100 - P["rsi_lo"]: return "short", a
    return None, a

# ---------- Konto / Positionen ----------
class Book:
    def __init__(s, d=None):
        d = d or {}
        s.balance = d.get("balance", P["start_equity"])
        s.peak = d.get("peak", s.balance)
        s.pos = d.get("pos", {})
        s.cool = d.get("cool", {})
        s.pause_until = d.get("pause_until", 0)
        s.killed = d.get("killed", False)
        s.trades = d.get("trades", [])
        s.last_px = d.get("last_px", {})

    def dump(s): return dict(balance=s.balance, peak=s.peak, pos=s.pos, cool=s.cool, pause_until=s.pause_until,
                             killed=s.killed, trades=s.trades, last_px=s.last_px)

    def upnl(s, px=None):
        px = px or s.last_px
        return sum(p["dir"] * p["qty"] * (px.get(sym, p["entry"]) - p["entry"]) for sym, p in s.pos.items())

    def equity(s, px=None): return s.balance + s.upnl(px)

    def open(s, sym, side, price, a, t):
        eq = s.equity()
        d = 1 if side == "long" else -1
        fill = price * (1 + d * P["slip"])
        stop_dist = min(P["stop_atr"] * a, P["max_stop_pct"] * fill)
        qty = (eq * P["risk"]) / stop_dist
        used = sum(p["qty"] * p["entry"] for p in s.pos.values())
        qty = min(qty, max(0, P["leverage"] * eq - used) / fill)
        if qty * fill < 10: return
        fee = qty * fill * P["fee"]; s.balance -= fee
        s.pos[sym] = dict(dir=d, qty=qty, entry=fill, stop=fill - d * stop_dist, best=fill, atr=a, t=t, bars=0, fees=fee)
        log(f"OPEN {side.upper()} {sym} {qty:.6f} @ {fill:.6g} | Stop {s.pos[sym]['stop']:.6g} | Exposure {qty*fill:.0f}$")

    def close(s, sym, price, t, why):
        p = s.pos.pop(sym); d = p["dir"]
        fill = price * (1 - d * P["slip"])
        fee = p["qty"] * fill * P["fee"]
        pnl = d * p["qty"] * (fill - p["entry"]) - fee
        s.balance += pnl
        net = pnl - p["fees"]
        s.trades.append(dict(sym=sym, side="long" if d == 1 else "short", entry=p["entry"], exit=fill, qty=p["qty"],
                             pnl=round(net, 4), why=why, t_in=p["t"], t_out=t))
        if net < 0: s.cool[sym] = t + P["cooldown_bars"] * P["bar_ms"]
        log(f"CLOSE {sym} @ {fill:.6g} ({why}) PnL {net:+.2f}$ | Kontostand {s.balance:.2f}$")

    def manage(s, sym, hi, lo, close, t, new_bar):
        """Stops gegen Hoch/Tief pruefen, Trailing-Stop nachziehen."""
        p = s.pos[sym]; d = p["dir"]
        if (d == 1 and lo <= p["stop"]) or (d == -1 and hi >= p["stop"]):
            return s.close(sym, p["stop"], t, "stop")
        p["best"] = max(p["best"], hi) if d == 1 else min(p["best"], lo)
        if d * (p["best"] - p["entry"]) >= P["trail_trigger_atr"] * p["atr"]:
            trail = p["best"] - d * P["trail_atr"] * p["atr"]
            be = p["entry"] * (1 + d * 2 * (P["fee"] + P["slip"]))  # mindestens Break-even
            new = max(trail, be) if d == 1 else min(trail, be)
            p["stop"] = max(p["stop"], new) if d == 1 else min(p["stop"], new)
        if new_bar:
            p["bars"] += 1
            if p["bars"] >= P["time_stop_bars"] and d * (close - p["entry"]) < 0.5 * p["atr"]:
                s.close(sym, close, t, "zeit")

    def risk_check(s, t):
        eq = s.equity(); s.peak = max(s.peak, eq)
        if eq <= P["start_equity"] * (1 - P["kill_dd"]) and not s.killed:
            s.killed = True; log(f"KILL-SWITCH: Kapital {eq:.2f}$ -> Bot stoppt Handel")
            return "kill"
        if eq <= s.peak * (1 - P["pause_dd"]) and t >= s.pause_until:
            s.pause_until = t + P["pause_hours"] * 3_600_000; s.peak = eq
            log(f"PAUSE {P['pause_hours']}h: Drawdown vom Hoch, Kapital {eq:.2f}$")
            return "pause"

    def can_enter(s, sym, t):
        return (not s.killed and t >= s.pause_until and sym not in s.pos
                and len(s.pos) < P["max_pos"] and t >= s.cool.get(sym, 0))

def summary(b, title, extra=""):
    eq = b.equity(); tr = b.trades
    wins = [x for x in tr if x["pnl"] > 0]
    lines = [f"# {title}", "", f"Stand: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", extra, "",
             f"- Kapital: **{eq:.2f} $** (Start {P['start_equity']:.0f} $, {100*(eq/P['start_equity']-1):+.2f} %)",
             f"- Realisiert: {b.balance:.2f} $ | offen: {b.upnl():+.2f} $",
             f"- Trades: {len(tr)} | Gewinner: {len(wins)} ({100*len(wins)/len(tr) if tr else 0:.0f} %)",
             f"- Status: {'GESTOPPT (Kill-Switch)' if b.killed else ('Pause' if b.pause_until > time.time()*1000 else 'aktiv')}",
             "", "## Offene Positionen"]
    for sym, p in b.pos.items():
        lines.append(f"- {sym} {'LONG' if p['dir']==1 else 'SHORT'} @ {p['entry']:.6g}, Stop {p['stop']:.6g}, jetzt {b.last_px.get(sym, 0):.6g}")
    if not b.pos: lines.append("- keine")
    lines += ["", "## Letzte Trades"] + [f"- {x['sym']} {x['side']} {x['pnl']:+.2f} $ ({x['why']})" for x in tr[-10:]]
    return "\n".join(lines) + "\n"

# ---------- Backtest ----------
def fetch_hist(sym, days):
    need, out, end = days * 86_400_000 // P["bar_ms"] + 60, [], None
    while len(out) < need:
        chunk = klines(sym, P["interval"], 1000, end)
        if not chunk: break
        out = chunk + out; end = chunk[0]["t"] - 1
        if len(chunk) < 1000: break
    return out[-need:]

def backtest(days):
    data = {s: fetch_hist(s, days) for s in SYMBOLS}
    n = min(len(v) for v in data.values())
    data = {s: v[-n:] for s, v in data.items()}
    b = Book(); curve = []
    global log
    _log = log; log = lambda m: None
    for i in range(60, n):
        t = data[SYMBOLS[0]][i]["ct"]
        for s in SYMBOLS:
            bar = data[s][i]; b.last_px[s] = bar["c"]
            if s in b.pos: b.manage(s, bar["h"], bar["l"], bar["c"], t, True)
        b.risk_check(t)
        for s in SYMBOLS:
            if b.can_enter(s, t):
                sig, a = signal(data[s][max(0, i-150):i+1])
                if sig: b.open(s, sig, data[s][i]["c"], a, t)
        curve.append(b.equity())
    for s in list(b.pos): b.close(s, b.last_px[s], 0, "ende")
    log = _log
    tr = b.trades; eq = b.balance
    mdd, pk = 0, P["start_equity"]
    for e in curve: pk = max(pk, e); mdd = max(mdd, 1 - e / pk)
    # 3-Tages-Fenster
    day = 86_400_000 // P["bar_ms"]; w = 3 * day
    rets = [curve[j+w]/curve[j]-1 for j in range(0, len(curve)-w, day)]
    print(f"Backtest {days} Tage, {len(SYMBOLS)} Coins: Ende {eq:.2f}$ ({100*(eq/P['start_equity']-1):+.1f} %), "
          f"Trades {len(tr)}, Trefferquote {100*sum(x['pnl']>0 for x in tr)/max(1,len(tr)):.0f} %, max. Drawdown {100*mdd:.1f} %")
    if rets: print(f"3-Tages-Fenster: bestes {100*max(rets):+.1f} %, schlechtestes {100*min(rets):+.1f} %, "
                   f"Median {100*sorted(rets)[len(rets)//2]:+.1f} %, positiv {sum(r>0 for r in rets)}/{len(rets)}")

# ---------- Live (Paper) ----------
def save(b, meta):
    d = b.dump(); d.update(meta)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f: json.dump(d, f, indent=1)
    os.replace(tmp, STATE)
    with open(TRADES, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["sym","side","entry","exit","qty","pnl","why","t_in","t_out"])
        w.writeheader(); w.writerows(b.trades)
    end = datetime.fromtimestamp(meta["end_ms"]/1000, timezone.utc)
    with open(STATUS, "w") as f: f.write(summary(b, "Trading-Bot Zwischenstand", f"Laeuft bis: {end:%Y-%m-%d %H:%M} UTC"))

def live(hours):
    d = json.load(open(STATE)) if os.path.exists(STATE) else {}
    b = Book(d)
    now = int(time.time() * 1000)
    meta = dict(start_ms=d.get("start_ms", now), end_ms=d.get("end_ms", now + int(hours * 3_600_000)), last_bar=d.get("last_bar", {}))
    log(f"Start/Fortsetzung. Kapital {b.equity():.2f}$, Ende {datetime.fromtimestamp(meta['end_ms']/1000, timezone.utc):%Y-%m-%d %H:%M} UTC")
    while True:
        now = int(time.time() * 1000)
        try:
            if now >= meta["end_ms"]:
                px = {x["symbol"]: float(x["price"]) for x in get("/api/v3/ticker/price?symbols=" + json.dumps(SYMBOLS, separators=(',', ':')))}
                b.last_px.update(px)
                for s in list(b.pos): b.close(s, px[s], now, "ende")
                save(b, meta); log(f"ENDE. Endkapital {b.balance:.2f}$"); return
            for s in SYMBOLS:
                bars = klines(s, P["interval"], 150)
                closed = [x for x in bars if x["ct"] < now]
                last = closed[-1]
                b.last_px[s] = bars[-1]["c"]
                new_bar = meta["last_bar"].get(s) != last["t"]
                if s in b.pos:
                    one = klines(s, "1m", 3)   # Intra-Bar-Stops genauer pruefen
                    b.manage(s, max(x["h"] for x in one), min(x["l"] for x in one), last["c"], now, new_bar)
                if new_bar:
                    meta["last_bar"][s] = last["t"]
                    if b.can_enter(s, now):
                        sig, a = signal(closed)
                        if sig: b.open(s, sig, bars[-1]["c"], a, now)
            b.risk_check(now)
            save(b, meta)
        except Exception as e:
            log(f"Fehler: {e!r}")
        time.sleep(30)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backtest", "live", "status"])
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--hours", type=float, default=72)
    a = ap.parse_args()
    if a.mode == "backtest": backtest(a.days)
    elif a.mode == "live": live(a.hours)
    else: print(open(STATUS).read() if os.path.exists(STATUS) else "noch kein Status")
