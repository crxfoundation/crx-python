"""Accept dedicated test requests: ``python -m crx.test_request_worker --help``."""

from __future__ import annotations

import argparse
import logging
import math
import sys

from .client import Client
from . import _maker
from ._test_requests import validate, word
from .errors import BadAnswer, BadRequest, CrxError
from .models import Trade

log = logging.getLogger("crx.test_requests")


class TestRequestWorker:
    """Use a dedicated taker client to accept only its own marked test requests.

    Each pass starts at the beginning of the account's event history. Current RFQ
    status decides whether an accept is permitted; no persisted cursor skips work.
    New accepts use the gateway's accept check and the standard trade signer.
    Accepted requests resume through the unsigned repeat route until the server
    reports a terminal status, including after the quote and request deadlines.
    """

    def __init__(self, client: Client, *, poll: float = 2, max_pages: int = 100):
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not math.isfinite(poll) or not 1 <= poll <= 60:
            raise BadRequest("poll must be from 1 to 60 seconds")
        if type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise BadRequest("max_pages must be from 1 to 1000")
        client._need_seat()
        self.client, self.poll, self.max_pages = client, float(poll), max_pages

    def run_once(self) -> list[Trade]:
        """Accept or resume requests in a bounded scan. Return newly signed trades."""
        c = self.client
        cursor, seen, results = 0, set(), []
        for _ in range(self.max_pages):
            rows, cursor = _maker._page(c, cursor)
            for event in rows:
                if not isinstance(event, dict) or event.get("type") != "rfq.opened":
                    continue
                d = event.get("data")
                try:
                    validate(d, c.chain_key, c._clock(), maker=False, event=True, historical_event=True)
                except BadAnswer:
                    continue
                rid = d["rfq_id"]
                if rid in seen:
                    continue
                seen.add(rid)
                try:
                    trade = self._accept(d)
                    if trade is not None:
                        results.append(trade)
                except CrxError as e:
                    log.warning("test request %s: %s", rid, e.code)
            if len(rows) < _maker.PAGE:
                return results
        raise BadAnswer("the test request event history exceeds max_pages")

    def _accept(self, event: dict) -> Trade | None:
        c, rid = self.client, event["rfq_id"]
        view = c._gw.request("GET", f"/rfqs/{rid}")
        validate(view, c.chain_key, c._clock(), maker=False, allow_accepted=True)
        for key in ("rfq_id", "client_rfq_id", "expiry", "closes_at", "request_expires_at", "taker_ref",
                    "quote_expiry_max"):
            if view.get(key) != event.get(key):
                raise BadAnswer("the test request changed after its opening event")
        if view["status"] == "accepted":
            self._recover(view)
            return None
        if view["status"] == "opened":
            return None
        if view.get("trade_status") not in (None, ""):
            return None
        if not word(view.get("leg_id")):
            raise BadAnswer("the test request has no acceptance leg")
        current = c._pick(view, rid)
        if current is None:
            return None
        qid = current.get("quote_id")
        if not word(qid):
            raise BadAnswer("the test quote has no quote id")
        check = c._gw.request("GET", f"/rfqs/{rid}/accept-check", query={"quote_id": qid})
        if (check.get("ok") is not True or check.get("rfq_id") != rid or check.get("quote_id") != qid
                or not isinstance(check.get("trade_template"), dict)):
            raise BadAnswer("the test quote did not pass the acceptance check")
        terms = dict(view, pair="USD/MXN", side="buy", premium_bps=0)
        quote = c._quote_of(terms, dict(current, trade_template=check["trade_template"]))
        if not quote.rate.is_finite() or quote.rate <= 0:
            raise BadAnswer("the test quote rate is invalid")
        return c.trade(quote)

    def _recover(self, view: dict) -> None:
        """Ask the server to submit its stored acceptance. Never sign another trade."""
        if view.get("trade_status") not in (None, "", "pending", "sending"):
            return
        quote = view.get("quote")
        if (not isinstance(quote, dict) or quote.get("status") != "accepted"
                or not word(quote.get("quote_id"))):
            raise BadAnswer("the test request has no accepted quote")
        rid, qid = view["rfq_id"], quote["quote_id"]
        answer = self.client._gw.request("POST", f"/rfqs/{rid}/accept", body={"quote_id": qid})
        if answer.get("rfq_id") != rid or answer.get("quote_id") != qid:
            raise BadAnswer("the test request recovery returned different identifiers")

    def run(self, *, duration: float = 60) -> None:
        """Poll for a bounded duration. Each HTTP request and trade has its own timeout."""
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration <= 0):
            raise BadRequest("duration must be a positive number of seconds")
        c = self.client
        end = c._clock() + duration
        while c._clock() < end:
            try:
                self.run_once()
            except _maker.TRANSIENT as e:
                log.warning("test request scan: %s", e.code)
            left = end - c._clock()
            if left > 0:
                c._sleep(min(self.poll, left))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Accept dedicated CRX test requests on Ethereum mainnet.",
        epilog="Set CRX_RPC for the Ethereum connection.",
    )
    parser.add_argument("--enable", action="store_true", required=True, help="Enable test request acceptance.")
    parser.add_argument("--key-file", required=True, help="Dedicated taker key file.")
    parser.add_argument("--state-dir", required=True, help="Dedicated SDK state directory.")
    parser.add_argument("--duration", type=float, default=60, help="Polling duration in seconds (default: 60).")
    parser.add_argument("--poll", type=float, default=2, help="Seconds between scans (default: 2).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    client = None
    try:
        client = Client(key_file=args.key_file, network="mainnet", allow_mainnet=True,
                        base_url="https://api.crxfx.com",
                        state_dir=args.state_dir, keepalive=None)
        TestRequestWorker(client, poll=args.poll).run(duration=args.duration)
    except CrxError as e:
        print(f"{e.code}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
