#!/usr/bin/env python3
"""Momentum-Rotation (Paper-Trading): Long die 3 staerksten, Short die 3 schwaechsten
von 12 Coins (Rendite der letzten 72 h). Alle 24 h neu gewichtet, 2x Gesamthebel,
marktneutral. Verlustbremse: -8 % Stop je Position, Kill-Switch bei -25 %.
  python xsmom.py live --hours 72 --dir runs/xsmom
"""
import argparse, json, os, time
import bot

LOOKBACK_H, TOP_K, GROSS, REBAL_H, STOP_PCT = 72, 3, 2.0, 24, 0.08

def ranking():
    r = {}
    for s in bot.SYMBOLS:
        k = bot.klines(s, "1h", LOOKBACK_H + 2)
        r[s] = k[-1]["c"] / k[-LOOKBACK_H - 1]["c"] - 1
    return sorted(r, key=r.get), r

def rebalance(b, now):
    order, r = ranking()
    target = {s: "long" for s in order[-TOP_K:]}; target.update({s: "short" for s in order[:TOP_K]})
    bot.log("Ranking 72h: " + ", ".join(f"{s[:-4]} {100*r[s]:+.1f}%" for s in reversed(order)))
    for s in list(b.pos):
        side = "long" if b.pos[s]["dir"] == 1 else "short"
        if target.get(s) != side: b.close(s, b.last_px[s], now, "rebalance")
    eq = b.equity(); per = eq * GROSS / (2 * TOP_K)
    for s, side in target.items():
        if s not in b.pos and not b.killed: b.open(s, side, b.last_px[s], 1e12, now, notional=per, stop_pct=STOP_PCT)

def live(hours):
    d = json.load(open(bot.STATE)) if os.path.exists(bot.STATE) else {}
    b = bot.Book(d)
    now = int(time.time() * 1000)
    meta = dict(start_ms=d.get("start_ms", now), end_ms=d.get("end_ms", now + int(hours * 3_600_000)),
                next_rebal=d.get("next_rebal", now), last_bar={})
    bot.log(f"Start/Fortsetzung Momentum-Rotation. Kapital {b.equity():.2f}$")
    stop_at = time.time() + bot.MAX_MIN * 60 if bot.MAX_MIN else None
    while True:
        now = int(time.time() * 1000)
        if stop_at and time.time() > stop_at:
            bot.save(b, meta); bot.log("Lauf-Pause (Job-Ende), Stand gespeichert"); return
        try:
            px = {x["symbol"]: float(x["price"]) for x in bot.get("/api/v3/ticker/price?symbols=" + json.dumps(bot.SYMBOLS, separators=(",", ":")))}
            b.last_px.update(px)
            if now >= meta["end_ms"]:
                for s in list(b.pos): b.close(s, px[s], now, "ende")
                bot.save(b, meta); bot.log(f"ENDE. Endkapital {b.balance:.2f}$"); return
            for s in list(b.pos):
                one = bot.klines(s, "1m", 3)
                b.manage(s, max(x["h"] for x in one), min(x["l"] for x in one), px[s], now, False)
            if b.risk_check(now) == "kill":
                for s in list(b.pos): b.close(s, px[s], now, "kill")
            if now >= meta["next_rebal"] and not b.killed and now >= b.pause_until:
                rebalance(b, now); meta["next_rebal"] = now + REBAL_H * 3_600_000
            bot.save(b, meta)
        except Exception as e:
            bot.log(f"Fehler: {e!r}")
        time.sleep(30)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live"]); ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--dir", default=os.path.join(bot.HERE, "runs", "xsmom"))
    ap.add_argument("--max-minutes", type=float, default=None)
    a = ap.parse_args()
    bot.MAX_MIN = a.max_minutes
    bot.STRATS["xsmom"] = dict(signal=None, symbols=bot.SYMBOLS12, title="Momentum-Rotation (72h, 24h-Rebalance)",
                               p=dict(time_stop_bars=10**9, max_pos=99, leverage=99))
    bot.use("xsmom", a.dir)
    live(a.hours)
