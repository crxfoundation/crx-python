"""The maker Quickstart, ready to run: ``python -m crx.quickstart_maker``.

Two Testnet accounts: account 1 quotes (the maker), account 2 asks (the test
taker). The test taker opens one RFQ in the background. The maker picks that RFQ
out of its stream, quotes it, and signs its Side once the taker accepts. Each
line starts with the local time. The steps are line for line the ones
``examples/maker.py`` and the maker Quickstart page print.

The maker key comes from ``CRX_WALLET_PK`` or ``CRX_WALLET_PK_FILE``, the test
taker key from ``CRX_TAKER_PK``. With one unset, the script asks for it and does
not show what you type. The keys then sit in this process's environment only.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading
import time
import uuid
from datetime import datetime

import crx

ENV_KEY = "CRX_WALLET_PK"
ENV_KEY_FILE = "CRX_WALLET_PK_FILE"
ENV_TAKER_KEY = "CRX_TAKER_PK"


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


def steps():
    """The seven steps of the maker Quickstart."""
    # 1. Connect. Account 1 quotes: CRX_WALLET_PK. Account 2 is your test taker: CRX_TAKER_PK.
    maker = crx.Client(network="testnet")
    taker = crx.Client(os.environ["CRX_TAKER_PK"], network="testnet")
    log(maker.address, taker.address)  # 21:04:52 0x7638…fe71 0x5b38…ddc4

    # 2. Fund both. You pay gas. A seat with no collateral receives no RFQs.
    maker.deposit(20_000)
    taker.deposit(20_000)
    log(maker.balance().free, taker.balance().free)  # 21:04:58 20000.000000 20000.000000

    # 3. Open the RFQ stream. It reads your tape up to now.
    rfqs = maker.rfqs(wait=60)

    # 4. Your test taker asks in the background. It accepts a maker quote only.
    cid = f"maker-qs-{uuid.uuid4().hex[:8]}"

    def ask():
        try:
            q = taker.quote("USD/MXN", "buy", 10_000, client_rfq_id=cid)
            log("taker: best quote", q.rate, "house" if q.house else "maker")
            if not q.house:
                log("taker:", taker.trade(q).status)  # 21:05:09 taker: open
        except crx.CrxError as e:
            log("taker:", e.code, e)

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
        log(rfq.pair, rfq.side, rfq.notional)  # 21:05:00 USD/MXN sell 10000
        q = maker.send_quote(rfq, near_mid(rfq))
        log("maker: quoted", q.rate)  # 21:05:01 maker: quoted 18.09991

        # 6. Confirm. Waits for the accept and signs your Side.
        #    CRX sends the tx and pays gas.
        t = maker.confirm(q, timeout=60)
        log("maker:", t.status)  # 21:05:09 maker: open
        break
    else:
        raise crx.NoQuotes("your test taker's RFQ did not reach the maker seat: check its maker role and collateral")

    # 7. Your trade, from the maker's side.
    asker.join(60)
    p = [p for p in maker.positions() if p.rfq_id == t.rfq_id][0]
    log(p.pair, p.side, p.notional, p.rate, p.status)
    # 21:05:10 USDMXN sell 10000 18.09991 open


def ask_key(name: str, file_name: str | None = None) -> bool:
    """Ask for the key ``name`` when it is not set. False when the reader gives none."""
    if os.environ.get(name) or (file_name and os.environ.get(file_name)):
        return True
    key = ""
    try:
        key = getpass.getpass(f"{name} (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        key = ""
    if not key:
        return False
    os.environ[name] = key
    key = ""
    return True


def main(argv: list[str] | None = None) -> int:
    """Run the maker Quickstart. 0 when every step ran, 1 on a CRX error, 2 with a key missing."""
    parser = argparse.ArgumentParser(
        prog="python -m crx.quickstart_maker",
        description="Run the CRX maker Quickstart on Testnet: your test taker asks, "
        "you quote, sign and open the trade.",
        epilog=f"The maker key comes from {ENV_KEY}, the test taker key from {ENV_TAKER_KEY}. "
        "Unset, the script asks for each and hides the input.",
    )
    parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    for name, file_name in ((ENV_KEY, ENV_KEY_FILE), (ENV_TAKER_KEY, None)):
        if not ask_key(name, file_name):
            print(f"No key. Set {name}=0x… and run again.", file=sys.stderr)
            return 2
    try:
        steps()
    except crx.CrxError as e:
        print(f"{e.code}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
