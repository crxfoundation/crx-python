from datetime import datetime, timezone

import crx


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


# 1. Connect. Reads your key from CRX_WALLET_PK.
c = crx.Client(network="testnet")
log(c.address)  # 11:20:54 0x7638…fe71

# 2. Fund. Mints any test USDC you lack, deposits. You pay gas.
d = c.deposit(20_000)
log(d.status)  # 11:21:20 credited

# 3. Balance. The deposit counts in a few seconds.
b = c.balance()
log(b.free, b.as_of)  # 11:21:20 20000.000000 2026-09-30 15:21:07+00:00

# 4. Request a quote. Settlement: 15 Dec 2026, 15:00 in São Paulo.
q = c.quote(
    "USD/BRL", "buy", 25_000,
    expiry=datetime(2026, 12, 15, 18, 0, tzinfo=timezone.utc),
)
log(q.pair, q.rate, q.house)  # 11:21:26 USD/BRL 5.2562 True
log(q.expiry)  # 11:21:26 2026-12-15 18:00:00+00:00
log(q.expires_at)  # 11:21:26 2026-09-30 15:21:52+00:00

# 5. Accept before q.expires_at. Signs your confirmation.
#    CRX sends the tx and pays gas.
t = c.trade(q)
log(t.status)  # 11:21:46 open

# 6. Your trade.
p = [p for p in c.positions() if p.rfq_id == t.rfq_id][0]
log(p.pair, p.side, p.notional, p.rate, p.status)
# 11:21:46 USDBRL buy 25000 5.2562 open
log(c.balance().im)  # 11:21:47 3065.908525

# 7. Withdraw. Signs the intent. CRX sends the tx and pays gas.
#    Leaves your balance at once.
#    In your wallet within about 2 hours.
w = c.withdraw(1_000)
log(w.status)  # 11:22:11 accepted
