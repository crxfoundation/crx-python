"""Accept the account's test requests: ``python -m crx.test_request_worker --help``."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys

from .client import Client
from . import _maker
from ._test_requests import validate, word
from .errors import BadAnswer, BadRequest, CrxError
from .models import Trade

log = logging.getLogger("crx.test_requests")

TERMINAL = ("settled", "expired", "cancelled")  # RFQ statuses that never trade again
DONE_FILE = "test-requests-done.json"
DONE_KEEP = 1000


class TestRequestWorker:
    """Accept the marked test requests of a dedicated taker client.

    Each scan reads the account's event history from the start and acts on each RFQ's current status.
    A new accept passes the gateway's accept check, then signs like any trade. An accepted request is
    posted again, unsigned, until it ends. Ended requests go to ``test-requests-done.json`` in the
    client state directory, and later scans skip them.

    Args:
        client: A taker client for a dedicated account.
        poll: Seconds between scans, from 1 to 60. Default 2.
        max_pages: The most ``/trades`` pages one scan reads, from 1 to 1000. Default 100.
    """

    def __init__(self, client: Client, *, poll: float = 2, max_pages: int = 100):
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not math.isfinite(poll) or not 1 <= poll <= 60:
            raise BadRequest("poll must be from 1 to 60 seconds")
        if type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise BadRequest("max_pages must be from 1 to 1000")
        client._need_seat()
        self.client, self.poll, self.max_pages = client, float(poll), max_pages
        self._done_path = client._state_dir / DONE_FILE
        self._done = self._load_done()
        self._said: dict[str, str] = {}

    def _load_done(self) -> list[str]:
        try:
            rows = json.loads(self._done_path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.warning("test request done list %s is unreadable; starting empty", self._done_path)
            return []
        return [r for r in rows if word(r)] if isinstance(rows, list) else []

    def _mark_done(self, rid: str) -> None:
        """Add ``rid`` to the done list and write it atomically. On a failed write, keep it in memory only."""
        self._done = [r for r in self._done if r != rid][-(DONE_KEEP - 1):] + [rid]
        tmp = self._done_path.with_name(f"{DONE_FILE}.{os.getpid()}.tmp")
        try:
            self._done_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._done, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._done_path)
        except OSError:
            log.warning("test request %s: done list not written; kept for this run", rid)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _say(self, rid: str, line: str) -> None:
        """Log a refusal for ``rid`` only when it differs from the last one."""
        if self._said.get(rid) != line:
            self._said[rid] = line
            log.warning("test request %s: %s", rid, line)

    def run_once(self) -> list[Trade]:
        """Scan once, and accept or resume each request. Return the trades signed in this scan.

        Raises:
            BadAnswer: The event history is longer than ``max_pages``.
        """
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
                if rid in seen or rid in self._done:
                    continue
                seen.add(rid)
                try:
                    trade = self._accept(d)
                    self._said.pop(rid, None)
                    if trade is not None:
                        results.append(trade)
                except CrxError as e:
                    self._say(rid, f"{e.code}: {e}")
            if len(rows) < _maker.PAGE:
                return results
        raise BadAnswer("the test request event history exceeds max_pages")

    def _accept(self, event: dict) -> Trade | None:
        c, rid = self.client, event["rfq_id"]
        view = c._gw.request("GET", f"/rfqs/{rid}")
        if isinstance(view, dict) and view.get("rfq_id") == rid and view.get("status") in TERMINAL:
            log.info("test request %s: %s", rid, view["status"])
            self._mark_done(rid)
            return None
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
        return c.trade(quote, wait=c._settle_wait)

    def _recover(self, view: dict) -> None:
        """Ask the gateway to submit the stored accept. Sign nothing."""
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
        """Scan every ``poll`` seconds until ``duration`` ends.

        Args:
            duration: Seconds to run, above 0. Default 60.
        """
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
