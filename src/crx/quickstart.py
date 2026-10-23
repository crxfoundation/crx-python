"""Run the API Quickstart on Testnet: ``python -m crx.quickstart``."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone

import crx
import crx._env

ENV_KEY = "CRX_WALLET_PK"
ENV_KEY_FILE = "CRX_WALLET_PK_FILE"


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


def step(n, label, route, result):
    """Print step ``n`` of 6: its name, the route it calls, its result."""
    method, path = route.split()
    log(f"{n}/6 {label:<18} {method:<4} {path:<21} -> {result}")


def short(value):
    """An id cut to its first 6 and last 4 characters."""
    return f"{value[:6]}...{value[-4:]}"


def steps():
    """Run the six Quickstart steps."""
    # Connect. Reads your key from CRX_WALLET_PK.
    c = crx.Client(network="testnet")
    log(c.address)  # 15:40:18 0x656e3a8c4f1d2b07e9a65c3d4b8f2e1a90c76bed

    # 1. Deposit. Mints any test USDC you lack, deposits. You pay the network fee.
    d = c.deposit(20_000)
    b = c.balance()
    step(1, "Deposit 20000 USDC", "POST /deposit", f"{d.status}, free {b.free}")
    # 15:40:42 1/6 Deposit 20000 USDC POST /deposit              -> credited, free 20000.000000

    # 2. Request a quote. Settlement: in 30 days, 15:00 in São Paulo.
    day = datetime.now(timezone.utc) + timedelta(days=30)
    while day.weekday() >= 5:  # a weekend rolls to Monday
        day += timedelta(days=1)
    when = day.replace(hour=18, minute=0, second=0, microsecond=0)
    q = c.quote("USD/BRL", "buy", 25_000, expiry=when)
    step(2, "Request a quote", "POST /rfqs", f"{q.pair} {q.rate}, rfq {short(q.rfq_id)}")
    # 15:40:52 2/6 Request a quote    POST /rfqs                 -> USD/BRL 5.2511, rfq 0x44fd...42f8

    # 3. Accept within 120 s of your request. Signs your confirmation.
    #    CRX sends the transaction and pays its fee.
    t = c.accept(q)
    step(3, "Accept", "POST /rfqs/{rfq_id}/accept", t.status)
    # 15:41:10 3/6 Accept             POST /rfqs/{rfq_id}/accept -> open

    # 4. Your trade: its entry price and unrealized P&L in USDC.
    p = [p for p in c.positions() if p.rfq_id == t.rfq_id][0]
    step(4, "Positions", "GET /positions",
         f"{p.pair} {p.side} {p.notional} at {p.entry_price}, {p.status}, P&L {p.unrealized_pnl}")
    # 15:41:10 4/6 Positions          GET  /positions            -> USD/BRL buy 25000 at 5.2511, open, P&L -1.204512

    # 5. The initial margin the trade holds.
    end = time.monotonic() + 300
    while (im := c.balance().im) is None and time.monotonic() < end:
        time.sleep(10)  # IM can read empty for a moment after the open
    step(5, "Margin (IM)", "GET /balance", f"IM {im}")
    # 15:41:10 5/6 Margin (IM)        GET  /balance              -> IM 3059.657629

    # 6. Withdraw. Signs the intent. CRX sends the transaction.
    w = c.withdraw(1_000)
    step(6, "Withdraw 1000 USDC", "POST /withdraw", w.status)
    # 15:41:34 6/6 Withdraw 1000 USDC POST /withdraw             -> accepted


def ask_key() -> bool:
    """Prompt for the key when none is set. Return False when none is entered."""
    try:
        env = crx._env.load()
    except crx.ConfigError:
        return True  # the client refuses the .env file and says why
    if env.get(ENV_KEY) or env.get(ENV_KEY_FILE):
        return True
    key = ""
    try:
        # Without a terminal getpass would echo the key, so ask nothing.
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
    """Run the Quickstart. Return 0 on success, 1 on a CRX error and 2 with no key."""
    parser = argparse.ArgumentParser(
        prog="python -m crx.quickstart",
        description="Run the CRX API Quickstart on Testnet: deposit, request a quote, "
        "accept, read positions and margin, withdraw.",
        epilog=f"Reads the key from {ENV_KEY}, or asks for it with hidden input.",
    )
    parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if not ask_key():
        print(f"No key. Set {ENV_KEY}=0x... and run again.", file=sys.stderr)
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
