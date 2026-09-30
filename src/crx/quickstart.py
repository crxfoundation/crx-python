"""The API Quickstart, ready to run: ``python -m crx.quickstart``.

Connects, funds, requests a quote, trades, reads the book and withdraws on
Testnet. Each line starts with the local time. The steps are line for line
the ones ``examples/quickstart.py`` and the API Quickstart page print.

The key comes from ``CRX_WALLET_PK`` or ``CRX_WALLET_PK_FILE``. With neither
set, the script asks for the key and does not show what you type. The key
then sits in this process's environment only.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import warnings
from datetime import datetime, timezone

import crx

ENV_KEY = "CRX_WALLET_PK"
ENV_KEY_FILE = "CRX_WALLET_PK_FILE"


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


def steps():
    """The seven steps of the API Quickstart."""
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


def ask_key() -> bool:
    """Ask for the key when no key is set. False when the reader gives none."""
    if os.environ.get(ENV_KEY) or os.environ.get(ENV_KEY_FILE):
        return True
    key = ""
    try:
        # No terminal to hide the input on: ask nothing.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass(f"{ENV_KEY} (hidden): ").strip()
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        key = ""
    if not key:
        return False
    os.environ[ENV_KEY] = key
    key = ""
    return True


def main(argv: list[str] | None = None) -> int:
    """Run the Quickstart. 0 when every step ran, 1 on a CRX error, 2 with no key."""
    parser = argparse.ArgumentParser(
        prog="python -m crx.quickstart",
        description="Run the CRX API Quickstart on Testnet: connect, fund, quote, "
        "trade, read your book, withdraw.",
        epilog=f"The key comes from {ENV_KEY}. Unset, the script asks for it and hides the input.",
    )
    parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if not ask_key():
        print(f"No key. Set {ENV_KEY}=0x… and run again.", file=sys.stderr)
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
