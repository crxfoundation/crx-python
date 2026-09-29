"""Markets, then one quote. Accepts nothing. Needs CRX_WALLET_PK."""
import crx

c = crx.Client(network="testnet")
for m in c.markets():
    if not m.paused:
        print(m.pair, "open" if m.open else f"closed, opens {m.next_open}", "min", m.min_notional)  # USD/BRL open min 10000

q = c.quote("USD/BRL", "sell", 50_000, wait=20)
print(q.pair, q.side, q.notional, "@", q.rate, "valid until", q.expires_at)  # USD/BRL sell 50000 @ 5.435 valid until 2026-09-29 14:03:12+00:00
