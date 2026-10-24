"""Run one maker test request on Ethereum mainnet: ``python -m crx.quickstart_maker``."""

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
import crx._env

ENV_KEY = "CRX_WALLET_PK"
ENV_KEY_FILE = "CRX_WALLET_PK_FILE"
COLLATERAL = Decimal("20")
# CRX refuses a deposit under 100 USDC.
MIN_DEPOSIT = Decimal("100")


def log(*values):
    print(datetime.now().strftime("%H:%M:%S"), *values)


def step(n, label, route, result):
    """Print step ``n`` of 4: its name, the route it calls, its result."""
    method, path = route.split()
    log(f"{n}/4 {label:<14} {method:<4} {path:<21} -> {result}")


def short(value):
    """An id cut to its first 6 and last 4 characters."""
    return f"{value[:6]}...{value[-4:]}"


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
    # Wait for a pending deposit before computing the shortfall.
    while _free(balance) < COLLATERAL and _deposit_status(balance) in ("sending", "pending"):
        _pause(end, poll, "deposit_timeout", "deposit credit was not confirmed before the deadline")
        balance = maker.balance()
    remaining = COLLATERAL - _free(balance)
    if remaining <= 0:
        step(1, "Deposit", "GET /balance", f"not required, free {_free(balance)}")
        return
    amount = max(remaining.quantize(Decimal("0.000001"), rounding=ROUND_UP), MIN_DEPOSIT)
    deposit = maker.deposit(amount, mint=False, wait=timeout)
    if deposit.status != "credited" or not deposit.counted:
        raise crx.CrxError("deposit credit was not confirmed before the deadline", code="deposit_timeout")
    step(1, "Deposit", "POST /deposit", f"{amount} USDC, free {_free(maker.balance())}")


def steps(*, credit_timeout: float = 1800, trade_timeout: float = 900, final_timeout: float = 1800,
          poll: float = 2) -> crx.Trade:
    """Deposit the shortfall, request and quote a test RFQ, and wait until the trade is open and final.

    Each timeout is in seconds. A step still waiting at its deadline raises ``CrxError``.
    """
    for value in (credit_timeout, trade_timeout, final_timeout, poll):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise crx.BadRequest("timeouts and poll must be positive seconds")
    maker = crx.Client(network="mainnet", base_url="https://api.crxfx.com", allow_mainnet=True)
    try:
        log(maker.address)
        _fund(maker, credit_timeout, poll)
        rfq = maker.test_request()
        step(2, "Test request", "POST /test-requests", f"{rfq.pair} {rfq.side} {rfq.notional}, rfq {short(rfq.rfq_id)}")
        # Use the returned reference rate as the example quote price.
        quote = maker.send_quote(rfq, rfq.reference_rate)
        step(3, "Quote", "POST /rfqs/{rfq_id}/quotes", f"quoted {quote.rate}")
        end = time.monotonic() + trade_timeout
        while True:
            left = max(0, end - time.monotonic())
            trade = maker.confirm(quote, timeout=left, poll=poll, wait=left)
            if trade.status == "open":
                break
            if trade.status not in ("pending", "sending"):
                raise crx.CrxError(f"the trade is {trade.status}", code="not_open")
            _pause(end, poll, "trade_timeout", "the trade did not open before the deadline")
        end = time.monotonic() + final_timeout
        while True:
            position = next((p for p in maker.positions() if p.rfq_id == trade.rfq_id), None)
            if position is not None and position.final:
                break
            _pause(end, max(poll, 15), "final_timeout", "the trade was not final before the deadline")
        step(4, "Final position", "GET /positions",
             f"{position.pair} {position.side} {position.notional} at {position.entry_price}, {position.status}, "
             f"final, P&L {position.unrealized_pnl}")
        return trade
    finally:
        maker.close()


def ask_key(name: str, file_name: str | None = None) -> bool:
    """Prompt for the key ``name`` when it is not set. Return False when none is entered."""
    try:
        env = crx._env.load()
    except crx.ConfigError:
        return True  # the client refuses the .env file and says why
    if env.get(name) or (file_name and env.get(file_name)):
        return True
    key = ""
    try:
        # Without a terminal getpass would echo the key, so ask nothing.
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
    """Run the maker quickstart. Return 0 only when the trade is open and final."""
    parser = argparse.ArgumentParser(
        prog="python -m crx.quickstart_maker",
        description="Run a 100 USD maker test request on Ethereum mainnet.",
        epilog=f"Set {ENV_KEY} or {ENV_KEY_FILE}. The script asks for a missing key with hidden input.",
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
