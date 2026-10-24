import time
from datetime import datetime, timedelta, timezone

import crx


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


def step(n, label, route, result):
    """Print step n of 6 with its name, route and result."""
    method, path = route.split()
    log(f"{n}/6 {label:<18} {method:<4} {path:<21} -> {result}")


def short(value):
    """Shorten an id to its first 6 and last 4 characters."""
    return f"{value[:6]}...{value[-4:]}"


# Read the key from CRX_WALLET_PK. Chain calls go to the gateway's /rpc route unless CRX_RPC is set.
c = crx.Client(network="mainnet", allow_mainnet=True,
               base_url="https://api.crxfx.com")
log(c.address)  # 15:40:18 0x656e3a8c4f1d2b07e9a65c3d4b8f2e1a90c76bed

# 1. Deposit USDC from the seat wallet. The wallet pays the network fee.
d = c.deposit(20_000)
b = c.balance()
step(1, "Deposit 20000 USDC", "POST /deposit", f"{d.status}, free {b.free}")
# 15:40:42 1/6 Deposit 20000 USDC POST /deposit              -> credited, free 20000.000000

# 2. Request a quote that settles in 30 days at 15:00 São Paulo time.
day = datetime.now(timezone.utc) + timedelta(days=30)
while day.weekday() >= 5:  # a weekend rolls to Monday
    day += timedelta(days=1)
when = day.replace(hour=18, minute=0, second=0, microsecond=0)
q = c.quote("USD/BRL", "buy", 25_000, expiry=when)
step(2, "Request a quote", "POST /rfqs", f"{q.pair} {q.rate}, rfq {short(q.rfq_id)}")
# 15:40:52 2/6 Request a quote    POST /rfqs                 -> USD/BRL 5.2511, rfq 0x44fd...42f8

# 3. Accept within 120 s of the request. CRX sends the transaction and pays its fee.
t = c.accept(q)
step(3, "Accept", "POST /rfqs/{rfq_id}/accept", t.status)
# 15:41:10 3/6 Accept             POST /rfqs/{rfq_id}/accept -> open

# 4. Read the entry price and unrealized P&L of the trade in USDC.
p = [p for p in c.positions() if p.rfq_id == t.rfq_id][0]
step(4, "Positions", "GET /positions",
     f"{p.pair} {p.side} {p.notional} at {p.entry_price}, {p.status}, P&L {p.unrealized_pnl}")
# 15:41:10 4/6 Positions          GET  /positions            -> USD/BRL buy 25000 at 5.2511, open, P&L -1.204512

# 5. Read the initial margin the trade holds.
end = time.monotonic() + 300
while (im := c.balance().im) is None and time.monotonic() < end:
    time.sleep(10)  # IM can read empty for a moment after the open
step(5, "Margin (IM)", "GET /balance", f"IM {im}")
# 15:41:10 5/6 Margin (IM)        GET  /balance              -> IM 3059.657629

# 6. Withdraw USDC. CRX sends the transaction and pays its fee.
w = c.withdraw(1_000)
step(6, "Withdraw 1000 USDC", "POST /withdraw", w.status)
# 15:41:34 6/6 Withdraw 1000 USDC POST /withdraw             -> accepted
