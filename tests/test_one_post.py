"""One post per signed body: a refusal goes back to the caller."""

import time

import pytest

import crx

from .conftest import Clock
from .test_at_most_once import withdraw_posts
from .test_money import Chain, balance_body, gate
from .test_quote_trade import clock  # noqa: F401 (fixtures)

# The gateway's 409 before it reads the latest settlement names the next request time.
UNREAD = {"error": "conflict: the latest settlement is not read yet; retry shortly", "code": "conflict",
          "outcome": "unavailable", "message": "Balances are updating. Next request after 17:05 UTC."}
WAIT = {"Retry-After": "240"}


def test_a_withdraw_409_is_posted_once_with_its_time(make_client, session, health, account):
    # wait=0 leaves no time to post the same intent again.
    Chain(session)
    g = gate(session, health, account)
    session.routes[("POST", "/withdraw")] = (409, UNREAD, WAIT)
    session.routes[("GET", "/balance")] = balance_body(account)
    clock = Clock(time.time())
    c = make_client(clock=clock)
    t0 = clock()
    with pytest.raises(crx.CrxError) as ei:
        c.withdraw(1000, wait=0)
    e = ei.value
    assert (e.code, e.status, str(e)) == ("conflict", 409, f"{UNREAD['message']} ({UNREAD['error']})")
    assert (e.details["retry_after_secs"], e.details["time_utc"], e.details["error"]) == (240, "17:05 UTC",
                                                                                          UNREAD["error"])
    assert len(withdraw_posts(session)) == 1 and clock() == t0 and c._held_withdraw is None
    session.routes[("POST", "/withdraw")] = g.answer
    session.routes[("GET", "/balance")] = [balance_body(account), g.view("accepted")]
    assert c.withdraw(1000).status == "accepted" and len(withdraw_posts(session)) == 2
