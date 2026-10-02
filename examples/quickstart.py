from datetime import datetime, timedelta, timezone

import crx


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


# 1. Connect. Reads your key from CRX_WALLET_PK.
c = crx.Client(network="testnet")
log(c.address)  # 10:36:16 0xe674…54df

# 2. Fund. Mints any test USDC you lack, deposits. You pay gas.
d = c.deposit(20_000)
log(d.status)  # 10:36:37 credited

# 3. Balance. The deposit counts in a few seconds.
b = c.balance()
log(b.free, b.as_of)  # 10:36:37 67988.118072 2026-10-02 14:36:23+00:00

# 4. Request a quote. Settlement: in 30 days, 15:00 in São Paulo.
day = datetime.now(timezone.utc) + timedelta(days=30)
while day.weekday() >= 5:  # a weekend rolls to Monday
    day += timedelta(days=1)
when = day.replace(hour=18, minute=0, second=0, microsecond=0)
q = c.quote("USD/BRL", "buy", 25_000, expiry=when)
log(q.pair, q.rate)  # 10:36:48 USD/BRL 5.2558
log(q.expiry)  # 10:36:48 2026-11-02 18:00:00+00:00
log(q.expires_at)  # 10:36:48 2026-10-02 14:46:07+00:00

# 5. Accept within 120 s of your request. Signs your confirmation.
#    CRX sends the tx and pays gas.
t = c.trade(q)
log(t.status)  # 10:37:05 open

# 6. Your trade.
p = [p for p in c.positions() if p.rfq_id == t.rfq_id][0]
log(p.pair, p.side, p.notional, p.rate, p.status)
# 10:37:05 USDBRL buy 25000 5.2558 open
log(c.balance().im)  # 10:37:05 12258.669289

# 7. Withdraw. Signs the intent. CRX sends the tx and pays gas.
#    Leaves your balance at once.
#    In your wallet within about 2 hours.
w = c.withdraw(1_000)
log(w.status)  # 10:37:23 accepted
