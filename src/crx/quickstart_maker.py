"""The maker Quickstart, ready to run: ``python -m crx.quickstart_maker``.

Two Testnet accounts: account 1 quotes (the maker), account 2 asks (the test
taker). The test taker opens one RFQ. The maker reads that RFQ off its stream
and quotes it. The test taker accepts the winning quote, and the maker confirms
the trade. The maker signs one binding quote and nothing after the accept. Each
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
import warnings
from datetime import datetime

import crx

ENV_KEY = "CRX_WALLET_PK"
ENV_KEY_FILE = "CRX_WALLET_PK_FILE"
ENV_TAKER_KEY = "CRX_TAKER_PK"


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


def steps():
    """The eight steps of the maker Quickstart."""
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


def ask_key(name: str, file_name: str | None = None) -> bool:
    """Ask for the key ``name`` when it is not set. False when the reader gives none."""
    if os.environ.get(name) or (file_name and os.environ.get(file_name)):
        return True
    key = ""
    try:
        # No terminal to hide the input on: ask nothing.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass(f"{name} (hidden): ").strip()
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
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
