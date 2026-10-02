"""A small JSON-RPC client and a checked transaction sender.

The RPC URL is kept out of reprs and errors. urllib3 DEBUG logs still show request paths.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests
from eth_utils import keccak, to_checksum_address

from .errors import BadAnswer, NetworkError, TxFailed, clean

REVERTS = {
    keccak(text=n + "()")[:4]: n
    for n in (
        "NotWhitelisted DepositsClosed ZeroAmount DepositBelowMinimum NotAdmin SeatNotSeeded AmountOverflow "
        "NotFunded DuplicateArm DeadlinePassed DeadlineTooFar BadSignature StopDepositsUndrained "
        "HardPaused BadTerms BadArmKind EnvelopeTooLong"
    ).split()
}


class RpcError(Exception):
    """The node answered with an error object."""

    def __init__(self, error: Any) -> None:
        super().__init__(clean(error))
        self.error = error


def revert_name(error: Any) -> str:
    if not isinstance(error, dict):
        return clean(error, 120)
    data = error.get("data")
    data = data.get("data") if isinstance(data, dict) else data
    if isinstance(data, str) and len(data) >= 10:
        try:
            return REVERTS.get(bytes.fromhex(data[2:10]), clean(data[:10]))
        except ValueError:
            return clean(data[:10])
    return clean(error.get("message", "a revert"), 120)


class Rpc:
    def __init__(self, url: str, session: requests.Session, timeout: float = 20) -> None:
        self._url = url
        self._session = session
        self._timeout = timeout

    def __repr__(self) -> str:
        return "Rpc(<url hidden>)"

    def __getstate__(self) -> object:
        raise TypeError("the RPC URL can hold a key; an Rpc cannot be pickled or copied")

    def __call__(self, method: str, *params: Any) -> Any:
        err: Exception | None = None
        r = body = None
        try:
            r = self._session.post(
                self._url, timeout=self._timeout,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
            )
        except requests.RequestException as e:
            err = NetworkError(f"the RPC did not answer ({type(e).__name__})")
        if err is None:
            try:
                body = r.json()
            except ValueError:  # requests' JSONDecodeError is a ValueError
                err = BadAnswer("the RPC sent an answer that is not JSON")
        if err is not None:
            # Raised outside the except block: no context carries the URL.
            raise err
        if not isinstance(body, dict):
            raise BadAnswer("the RPC sent an answer that is not an object")
        if "error" in body:
            raise RpcError(body["error"])
        if "result" not in body:
            raise BadAnswer("the RPC answer has no result")
        return body["result"]

    def int(self, method: str, *params: Any) -> int:
        v = self(method, *params)
        try:
            return int(v, 16)
        except (TypeError, ValueError):
            raise BadAnswer(f"the RPC sent a {method} answer that is not a number") from None


def send_tx(
    rpc: Rpc,
    account: Any,
    chain_id: int,
    what: str,
    to: str,
    data: str,
    *,
    gas: int | None = None,
    wait_s: float = 120,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Simulate, sign, send, and wait for status 1. Returns the tx hash."""
    sender = account.address
    call = {"from": sender, "to": to, "data": data}
    try:
        rpc("eth_call", call, "latest")
        if gas is None:
            gas = rpc.int("eth_estimateGas", call) * 12 // 10
    except RpcError as e:
        raise TxFailed(f"{what} would revert: {revert_name(e.error)}; nothing sent", details={"step": what}) from None
    signed = account.sign_transaction(
        {
            "chainId": int(chain_id),
            "nonce": rpc.int("eth_getTransactionCount", sender, "pending"),
            "to": to_checksum_address(to),
            "data": data,
            "value": 0,
            "gas": int(gas),
            "gasPrice": rpc.int("eth_gasPrice"),
        }
    )
    raw = "0x" + bytes(signed.raw_transaction).hex()
    try:
        tx = rpc("eth_sendRawTransaction", raw)
    except RpcError as e:
        raise TxFailed(f"{what} not sent: {clean(e.error, 160)}", details={"step": what}) from None
    except NetworkError:
        # The node may hold it. Its own hash is the handle.
        tx = "0x" + bytes(signed.hash).hex()
    deadline = time.monotonic() + wait_s
    while True:
        try:
            receipt = rpc("eth_getTransactionReceipt", tx)
        except (RpcError, NetworkError):
            receipt = None
        if receipt:
            if receipt.get("status") != "0x1":
                raise TxFailed(f"{what} tx {tx} reverted", details={"step": what, "tx": tx})
            return tx
        if time.monotonic() > deadline:
            raise TxFailed(f"{what} tx {tx} has no receipt after {int(wait_s)} s", details={"step": what, "tx": tx})
        sleep(2)
