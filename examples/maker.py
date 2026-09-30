"""The maker Quickstart. Needs two Testnet accounts: CRX_WALLET_PK quotes, CRX_TAKER_PK asks."""
import os
import threading
import time
import uuid
from datetime import datetime

import crx


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


# 1. Connect. Account 1 quotes: CRX_WALLET_PK. Account 2 is your test taker: CRX_TAKER_PK.
maker = crx.Client(network="testnet")
taker = crx.Client(os.environ["CRX_TAKER_PK"], network="testnet")
log(maker.address, taker.address)  # 21:04:52 0x7638…fe71 0x5b38…ddc4

# 2. Fund both. You pay gas. A seat with no collateral receives no RFQs.
maker.deposit(20_000)
taker.deposit(20_000)
log(maker.balance().free, taker.balance().free)  # 21:04:58 20000.000000 20000.000000

# 3. Open the RFQ stream. It reads your tape up to now,
#    and ends when your test taker is done.
taker_done = threading.Event()
taker_failed = []
rfqs = maker.rfqs(wait=60, stop=taker_done)

# 4. Your test taker asks in the background: 25,000 USD, the least a Testnet
#    account may ask. It accepts a maker quote only.
cid = f"maker-qs-{uuid.uuid4().hex[:8]}"

def ask():
    try:
        q = taker.quote("USD/MXN", "buy", 25_000, client_rfq_id=cid)
        log("taker: best quote", q.rate, "house" if q.house else "maker")
        if not q.house:
            log("taker:", taker.trade(q).status)  # 21:05:09 taker: open
    except crx.CrxError as e:
        log("taker:", e.code, e)
        taker_failed.append(e)
    finally:
        taker_done.set()

asker = threading.Thread(target=ask, daemon=True)
asker.start()

# Other desks ask on Testnet too. Quote your test taker's RFQ only:
# the gateway gives client_rfq_id back to an RFQ's own taker, no one else.
def asked_by_taker(rfq):
    try:
        return taker.rfq(rfq.rfq_id).client_rfq_id == cid
    except crx.AuthError:
        return False

# A near-mid rate: the house desk's rate on this RFQ, as your test taker reads it.
# Any maker quote outranks a house quote.
def near_mid(rfq):
    for _ in range(12):
        rate = taker.rfq(rfq.rfq_id).house_rate
        if rate:
            return rate
        time.sleep(0.25)
    raise crx.NoQuotes("no house quote to price from")

# 5. Quote your test taker's RFQ. Signs your Leg. Quote fast: a few seconds
#    after the RFQ opens, the gateway ranks the quotes and the taker gets the best one.
for rfq in rfqs:
    if not asked_by_taker(rfq):
        continue
    log(rfq.pair, rfq.side, rfq.notional)  # 21:05:00 USD/MXN sell 25000
    q = maker.send_quote(rfq, near_mid(rfq))
    log("maker: quoted", q.rate)  # 21:05:01 maker: quoted 18.09991

    # 6. Confirm. Waits for the accept and signs your Side.
    #    CRX sends the tx and pays gas.
    t = maker.confirm(q, timeout=60)
    log("maker:", t.status)  # 21:05:09 maker: open
    break
else:
    # The taker's own refusal ends the run: its RFQ never opened.
    if taker_failed and not isinstance(taker_failed[0], crx.NoQuotes):
        raise taker_failed[0]
    raise crx.NoQuotes("your test taker's RFQ did not reach the maker seat: check its maker role and collateral")

# 7. Your trade, from the maker's side.
asker.join(60)
p = [p for p in maker.positions() if p.rfq_id == t.rfq_id][0]
log(p.pair, p.side, p.notional, p.rate, p.status)
# 21:05:10 USDMXN sell 25000 18.09991 open
