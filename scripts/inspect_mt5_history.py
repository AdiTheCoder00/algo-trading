"""What the Vantage demo account actually did - real deals, not a backtest."""
import collections
import datetime as dt

import MetaTrader5 as mt5

mt5.initialize()
a = mt5.account_info()
print(f"account {a.login} {a.server} | balance {a.balance:.2f} equity {a.equity:.2f} {a.currency}")

# Bounds only - MT5 converts these to Unix time, so UTC-aware is both accepted
# and unambiguous. Deal timestamps below are rendered in UTC for the same reason:
# the terminal shows server time, and mixing the two in one report is how an
# hour of trades gets attributed to the wrong session.
frm = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
to = dt.datetime.now(dt.UTC) + dt.timedelta(days=2)
deals = mt5.history_deals_get(frm, to) or []
print(f"deals in history: {len(deals)}")

# Rebuild round trips: an OUT deal carries the realised P&L of the position it closed.
rows = []
for d in deals:
    if d.entry != mt5.DEAL_ENTRY_OUT:
        continue
    rows.append({
        "ts": dt.datetime.fromtimestamp(d.time, dt.UTC),
        "symbol": d.symbol, "magic": d.magic, "volume": d.volume, "price": d.price,
        "profit": d.profit, "swap": d.swap, "comm": d.commission,
        "net": d.profit + d.swap + d.commission,
        "comment": d.comment, "position": d.position_id,
    })
rows.sort(key=lambda r: r["ts"])
print(f"closed round trips: {len(rows)}")
if not rows:
    mt5.shutdown()
    raise SystemExit("no closed trades in history")

print(f"span: {rows[0]['ts']:%Y-%m-%d} .. {rows[-1]['ts']:%Y-%m-%d}\n")

tot = sum(r["net"] for r in rows)
wins = [r for r in rows if r["net"] > 0]
losses = [r for r in rows if r["net"] <= 0]
print(f"net {tot:+,.2f} | {len(wins)} wins / {len(losses)} losses "
      f"({100*len(wins)/len(rows):.1f}% win rate)")
gp = sum(r["net"] for r in wins)
gl = -sum(r["net"] for r in losses)
print(f"gross profit {gp:,.2f} | gross loss {gl:,.2f} | PF {(gp/gl if gl else 0):.2f}")
print(
    f"swap paid {sum(r['swap'] for r in rows):+,.2f} | "
    f"commission {sum(r['comm'] for r in rows):+,.2f}\n"
)

print("=== by magic (which expert) ===")
by = collections.defaultdict(list)
for r in rows:
    by[r["magic"]].append(r)
for m, rs in sorted(by.items(), key=lambda kv: sum(x["net"] for x in kv[1])):
    n = sum(x["net"] for x in rs)
    w = sum(1 for x in rs if x["net"] > 0)
    worst = min(x["net"] for x in rs)
    print(f"  magic {m:<10} {len(rs):>4} trades  net {n:>11,.2f}  "
          f"win {100*w/len(rs):>5.1f}%  worst {worst:>10,.2f}")

print("\n=== by symbol ===")
bys = collections.defaultdict(list)
for r in rows:
    bys[r["symbol"]].append(r)
for s, rs in sorted(bys.items(), key=lambda kv: sum(x["net"] for x in kv[1])):
    n = sum(x["net"] for x in rs)
    worst = min(x["net"] for x in rs)
    print(f"  {s:<12} {len(rs):>4} trades  net {n:>11,.2f}  worst {worst:>10,.2f}")

print("\n=== the 15 biggest losses ===")
print(f"{'when':<17}{'symbol':<12}{'magic':<10}{'vol':>6}{'net':>10}{'swap':>9}  comment")
for r in sorted(rows, key=lambda r: r["net"])[:15]:
    print(f"{r['ts']:%Y-%m-%d %H:%M}  {r['symbol']:<12}{r['magic']:<10}"
          f"{r['volume']:>6.2f}{r['net']:>10,.2f}{r['swap']:>9,.2f}  "
          f"{r['comment'][:34]}")

print("\n=== loss size distribution ===")
buckets = [(0, 5), (5, 15), (15, 30), (30, 60), (60, 120), (120, 1e9)]
for lo, hi in buckets:
    sel = [r for r in losses if lo <= -r["net"] < hi]
    if not sel:
        continue
    share = sum(-r["net"] for r in sel)
    label = f"{lo:.0f}-{hi:.0f}" if hi < 1e9 else f"{lo:.0f}+"
    print(f"  loss {label:>9}: {len(sel):>4} trades, {share:>10,.2f} total "
          f"({100*share/gl:>5.1f}% of all losses)")

print("\n=== how much of the total loss the worst N trades caused ===")
ordered = sorted(losses, key=lambda r: r["net"])
run = 0.0
for k in (1, 3, 5, 10, 20):
    if k > len(ordered):
        break
    run = -sum(r["net"] for r in ordered[:k])
    print(f"  worst {k:>3}: {run:>10,.2f}  = {100*run/gl:>5.1f}% of all losses")

print("\n=== comments on losing trades (what closed them) ===")
cc = collections.Counter((r["comment"] or "(none)")[:26] for r in losses)
for c, n in cc.most_common(10):
    tot_c = sum(r["net"] for r in losses if (r["comment"] or "(none)")[:26] == c)
    print(f"  {n:>4}x  {tot_c:>11,.2f}  {c}")

mt5.shutdown()
