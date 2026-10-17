"""Run one maker test request: ``python -m crx.quickstart_maker``.

Use an approved maker account on Ethereum mainnet. Set CRX_WALLET_PK or
CRX_WALLET_PK_FILE, and CRX_RPC. The account needs USDC and ETH for its deposit.
Under 20 USDC of free collateral, the example deposits the shortfall, at least the
100 USDC minimum deposit.
The returned reference rate is an example price for this 100 USD test request.
Quote the returned RFQ promptly: it closes after 120 seconds. The request
can renew an unaccepted RFQ for three hours, with the same settlement date.
"""

from __future__ import annotations

import argparse
import getpass
import math
import os
import sys
import time
import warnings
from datetime import datetime
from decimal import Decimal, ROUND_UP

import crx

ENV_KEY = "CRX_WALLET_PK"
ENV_KEY_FILE = "CRX_WALLET_PK_FILE"
COLLATERAL = Decimal("20")
# The core refuses a deposit under 100 USDC.
MIN_DEPOSIT = Decimal("100")


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


def _free(balance) -> Decimal:
    value = balance.free
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise crx.BadAnswer("the free collateral balance is unavailable")
    return value


def _deposit_status(balance) -> str | None:
    deposit = balance.raw.get("deposit")
    last = deposit.get("last") if isinstance(deposit, dict) else None
    return last.get("status") if isinstance(last, dict) else None


def _pause(end: float, poll: float, code: str, message: str) -> None:
    left = end - time.monotonic()
    if left <= 0:
        raise crx.CrxError(message, code=code)
    time.sleep(min(poll, left))


def _fund(maker, timeout: float, poll: float) -> None:
    end = time.monotonic() + timeout
    balance = maker.balance()
    # Wait for a previous deposit before calculating any remaining amount.
    while _free(balance) < COLLATERAL and _deposit_status(balance) in ("sending", "pending"):
        _pause(end, poll, "deposit_timeout", "deposit credit was not confirmed before the deadline")
        balance = maker.balance()
    remaining = COLLATERAL - _free(balance)
    if remaining > 0:
        amount = max(remaining.quantize(Decimal("0.000001"), rounding=ROUND_UP), MIN_DEPOSIT)
        deposit = maker.deposit(amount, mint=False)
        if deposit.status == "failed":
            raise crx.CrxError("the deposit failed", code="deposit_failed")
        balance = maker.balance()
        while _free(balance) < COLLATERAL:
            if _deposit_status(balance) == "failed":
                raise crx.CrxError("the deposit failed", code="deposit_failed")
            _pause(end, poll, "deposit_timeout", "deposit credit was not confirmed before the deadline")
            balance = maker.balance()
    log("free collateral", _free(balance), "USDC")


def steps(*, credit_timeout: float = 300, trade_timeout: float = 300, final_timeout: float = 1800,
          poll: float = 2) -> crx.Trade:
    """Deposit the shortfall (at least 100 USDC), request and quote a test RFQ, wait until the trade is open,
    then read it back from ``/positions`` until it is final and print it.

    A pending deposit, a pending trade or a trade not final at its deadline raises an error. Individual SDK
    calls also have their own network and confirmation timeouts.
    """
    for value in (credit_timeout, trade_timeout, final_timeout, poll):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise crx.BadRequest("timeouts and poll must be positive seconds")
    maker = crx.Client(network="mainnet", base_url="https://api.crxfx.com", allow_mainnet=True)
    try:
        log(maker.address)
        _fund(maker, credit_timeout, poll)
        rfq = maker.test_request()
        log(rfq.pair, rfq.side, rfq.notional)
        # Use the returned reference rate as the example quote price.
        quote = maker.send_quote(rfq, rfq.reference_rate)
        log("maker: quoted", quote.rate)
        end = time.monotonic() + trade_timeout
        while True:
            trade = maker.confirm(quote, timeout=max(0, end - time.monotonic()), poll=poll)
            if trade.status == "open":
                log("maker: open")
                break
            if trade.status not in ("pending", "sending"):
                raise crx.CrxError(f"the trade is {trade.status}", code="not_open")
            _pause(end, poll, "trade_timeout", "the trade did not open before the deadline")
        # Read the trade back from the API until it is final.
        end = time.monotonic() + final_timeout
        while True:
            position = next((p for p in maker.positions() if p.rfq_id == trade.rfq_id), None)
            if position is not None and position.final:
                break
            _pause(end, max(poll, 15), "final_timeout", "the trade was not final before the deadline")
        log(position.pair, position.side, position.notional, position.rate, position.status, "final")
        return trade
    finally:
        maker.close()


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
    """Run the maker quickstart. Return zero only when the trade is open and final."""
    parser = argparse.ArgumentParser(
        prog="python -m crx.quickstart_maker",
        description="Run a 100 USD maker test request on Ethereum mainnet.",
        epilog=f"Set {ENV_KEY} or {ENV_KEY_FILE}, and CRX_RPC. A missing key is requested with hidden input.",
    )
    parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if not ask_key(ENV_KEY, ENV_KEY_FILE):
        print(f"Set {ENV_KEY} or {ENV_KEY_FILE}.", file=sys.stderr)
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
