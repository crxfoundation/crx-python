from datetime import datetime, timezone

import crx


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


# 1. Connect. Reads your key from CRX_WALLET_PK.
c = crx.Client(network="testnet")
log(c.address)  # 21:04:52 0x7638…fe71

# 2. Fund. Mints any test USDC you lack, deposits. You pay gas.
d = c.deposit(20_000)
log(d.status)  # 21:04:55 credited

# 3. Balance. The deposit counts in a few seconds.
b = c.balance()
log(b.free, b.as_of)  # 21:04:57 20000.000000 2026-09-29 21:04:57+00:00

# 4. Request a quote. Settlement: 15 Dec 2026, 15:00 in São Paulo.
q = c.quote(
    "USD/BRL", "buy", 100_000,
    expiry=datetime(2026, 12, 15, 18, 0, tzinfo=timezone.utc),
)
log(q.pair, q.rate, q.house)  # 21:04:59 USD/BRL 5.2902 True
log(q.expiry)  # 21:04:59 2026-12-15 18:00:00+00:00
log(q.expires_at)  # 21:04:59 2026-09-29 21:08:30+00:00

# 5. Accept before q.expires_at. Signs your confirmation.
#    CRX sends the tx and pays gas.
t = c.trade(q)
log(t.status)  # 21:05:03 open

# 6. Your trade.
p = [p for p in c.positions() if p.rfq_id == t.rfq_id][0]
log(p.pair, p.side, p.notional, p.rate, p.status)
# 21:05:04 USDBRL buy 100000 5.2902 open
log(c.balance().im)  # 21:05:04 12221.225374

# 7. Withdraw. Signs the intent. CRX sends the tx and pays gas.
#    Leaves your balance at once.
#    In your wallet within about 2 hours.
w = c.withdraw(1_000)
log(w.status)  # 21:05:07 accepted
