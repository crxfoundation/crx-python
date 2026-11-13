"""A gateway release that serves money terms other than the caller's gets no signature and no signed body."""

import copy

import pytest

import crx

from .test_money import Chain, balance_body, deposit_route, gate
from .test_quote_trade import clock, put, refused_unsigned, venue  # noqa: F401 (fixtures)
from .conftest import WEEK

OTHER = "0x" + "99" * 20


def plus(n):
    """Return an edit that adds ``n`` to a decimal string member."""
    return lambda v: str(int(v) + n)


# Each signed money member of the served Trade, set off the caller's terms. message.notionalE6 is in
# test_quote_trade.SERVED_EDITS (test_served_typed_data_must_equal_own).
SERVED_OFF_TERMS = {
    "message.pair": "USD/BRL",
    "message.side": "sell",
    "message.rateE6": plus(1),
    "message.premiumBps": "5",
    "message.maturity": plus(86_400),
    "message.summary": lambda s: s.replace("buy", "sell"),
    "domain.chainId": 1,
    "domain.verifyingContract": OTHER,
}


def test_a_served_trade_off_the_callers_terms_is_refused_and_nothing_signed_goes_out(
        make_client, venue, session, clock, account):
    for at, value in SERVED_OFF_TERMS.items():
        venue.reset()
        venue.typed_edit = put(at, value)
        c = make_client(clock=clock)
        q = c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
        with pytest.raises(crx.RefusedToSign, match=f"^refused to sign: the served typed_data differs at {at}$"):
            c.trade(q)
        assert refused_unsigned(session, c, account), at
    # A Trade rebuilt whole at a rate other than the quote's, summary included, is refused too.
    venue.reset()
    venue.typed_edit, venue.rate, venue.row_edit = None, "18.900000", {"rate": "18.700000"}
    c = make_client(clock=clock)
    q = c.quote("USD/MXN", "buy", 25_000, settles=WEEK)
    assert str(q.rate) == "18.700000"
    with pytest.raises(crx.RefusedToSign, match="^refused to sign: the served typed_data differs at message.summary$"):
        c.trade(q)
    assert refused_unsigned(session, c, account)


# /health that names another core or domain. Another chain id: test_rpc_gateway
# test_the_health_pins_stand_and_no_chain_probe_goes_to_the_gateway.
HEALTH_MOVES = [
    ("core", OTHER, "^core moved: /health names 0x9999"),
    ("domain", "0x" + "77" * 32, "^domain moved: /health does not match the core; nothing signed$"),
]


def trade(c):
    c.trade(c.quote("USD/MXN", "buy", 25_000, settles=WEEK))


def test_every_signing_call_refuses_a_health_that_moves_the_core_or_domain(
        make_client, venue, session, health, clock, account):
    Chain(session, held=10**12)
    session.routes[("GET", "/balance")] = balance_body(account)
    g = gate(session, health, account)
    deposit_route(session, health, account, 1000 * 10**6)
    calls = {"trade": trade, "withdraw": lambda c: c.withdraw(1000), "deposit": lambda c: c.deposit(1000)}
    for member, value, words in HEALTH_MOVES:
        moved = copy.deepcopy(health)
        next(ch for ch in moved["chains"] if ch["key"] == "avax-fuji")[member] = value
        session.routes[("GET", "/health")] = moved
        for name, call in calls.items():
            venue.reset()
            c = make_client(clock=clock)
            with pytest.raises(crx.RefusedToSign, match=words):
                call(c)
            assert refused_unsigned(session, c, account), (member, name)
    assert g.intent is None and "/withdraw" not in session.paths("POST")
