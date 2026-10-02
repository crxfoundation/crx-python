"""The maker Quickstart. Needs two Testnet accounts:
CRX_WALLET_PK quotes, CRX_TAKER_PK asks."""
import os
from datetime import datetime

import crx


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


# 1. Connect. Account 1 quotes: CRX_WALLET_PK.
#    Account 2 is your test taker: CRX_TAKER_PK.
maker = crx.Client(network="testnet")
taker = crx.Client(os.environ["CRX_TAKER_PK"], network="testnet")
log(maker.address, taker.address)
# 12:36:07 0x8acb…7b5e 0x9fcc…3840

# 2. Fund both. You pay gas. An account with no collateral gets no RFQs.
maker.deposit(20_000)
taker.deposit(20_000)
log(maker.balance().free, taker.balance().free)
# 12:36:52 34024.610739 173215.983796

# 3. Your USD/MXN rate. Replace it with your own price.
RATE = "18.12"

# 4. Your test taker asks.
ask = taker.ask("USD/MXN", "buy", 25_000)  # POST /rfqs

# 5. Quote your test taker's RFQ. Your key signs a binding quote.
#    Quote within 10 s.
rfq = next(maker.rfqs(only=ask, wait=10))
log(rfq.pair, rfq.side, rfq.notional)  # 12:36:52 USD/MXN sell 25000
q = maker.send_quote(rfq, RATE)  # POST /rfqs/{rfq_id}/quotes
log("maker: quoted", q.rate)  # 12:36:52 maker: quoted 18.12

# 6. Your test taker gets the winning quote. It accepts yours only.
won = ask.quote()
mine = won.quote_id == q.quote_id
log("taker: winning quote", won.rate, "yours" if mine else "not yours")
# 12:37:03 taker: winning quote 18.12 yours
if mine:
    t = taker.trade(won)  # POST /rfqs/{rfq_id}/accept
    log("taker:", t.status)  # 12:37:34 taker: pending

# 7. Read the accept. Your quote is your signature:
#    you sign nothing more. CRX sends the tx and pays gas.
#    No accept in 60 s: cancel your quote.
try:
    t = maker.confirm(q, timeout=60)
except crx.QuoteLost as e:
    if e.reason == "timeout":
        maker.drop_quote(q)  # DELETE /rfqs/{rfq_id}/quotes/{leg_id}
    raise
log("maker:", t.status)  # 12:38:04 maker: pending

# 8. Pending: accepted, the tx landed, and the trade opens at the
#    next hourly check.
if t.status == "pending" and t.tx:
    log("maker: accepted; it opens at the next hourly check,",
        f"{maker.next_check().astimezone():%H:%M}")
    # 12:38:04 maker: accepted; it opens at the next hourly check, 13:05
elif t.status != "open":
    raise crx.CrxError(
        f"the trade is {t.status}, not open", code="not_open")
