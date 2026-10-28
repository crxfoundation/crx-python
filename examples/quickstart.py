import crx
from datetime import datetime, timedelta, timezone

# Reads CRX_WALLET_PK_FILE (or CRX_WALLET_PK).
# crx.Client(network="mainnet", allow_mainnet=True)
c = crx.Client(network="mainnet", allow_mainnet=True)

# c.deposit(amount)
d = c.deposit(100)  # returns once CRX validates the deposit
print("Validated" if d.counted else d.status, c.balance().free)

# c.quote(pair="USD/MXN" | "USD/BRL" | "USD/PHP" | "USD/INR" | "BTC/USD" | "EUR/USD", side="buy" | "sell", notional=USD 100 to 10_000_000 | BTC or EUR in c.markets() limits, settles=aware datetime | "YYYY-MM-DDTHH:MMZ" | Unix seconds)
settles = datetime.now(timezone.utc) + timedelta(hours=2)
q = c.quote("USD/MXN", "buy", 100, settles=settles)
print(q.pair, q.side, q.notional, q.rate, q.settles_at, q.closes_at)

# c.accept(quote) · c.reject(quote)
if q.rate <= 19.5:  # the most MXN you pay for 1 USD
    t = c.accept(q)  # returns once the trade is open or refused
    print(t.status)
else:
    c.reject(q)

# c.positions()
for p in c.positions():
    print(p.pair, p.side, p.notional, p.entry_price,
          p.settles_at, p.unrealized_pnl)

# c.withdraw(amount=None)
w = c.withdraw(50)  # c.withdraw() withdraws all free collateral
print(w.amount, w.status)
