"""USDC/USDT: the name a person reads for the API code USD/UST."""

import copy

import pytest

import crx

from .conftest import WEEK
from .test_quote_trade import Venue, accepts, clock, recovers  # noqa: F401 (fixture)


def with_ust(markets):
    """Return /markets with a USD/UST row, a copy of the USD/MXN row."""
    m = copy.deepcopy(markets)
    row = copy.deepcopy(next(r for r in m["markets"] if r["pair"] == "USD/MXN"))
    m["markets"].append({**row, "pair": "USD/UST"})
    return m


def test_usdc_usdt_trades_usd_ust_with_the_api_code_in_request_fields_and_signature(
        make_client, session, health, markets, clock, account):
    venue = Venue(session, health, with_ust(markets), clock, pair="USD/UST")
    c = make_client(clock=clock)
    q = c.quote("USDC/USDT", "buy", 25_000, settles=WEEK)
    assert (venue.rfq_body["pair"], q.pair, c.market("usdcusdt").pair) == ("USDUST", "USD/UST", "USD/UST")
    t = c.trade(q)
    (body,) = [b for b in accepts(session) if "sig" in b]
    assert (t.status, t.pair, venue.template["typed_data"]["message"]["pair"]) == ("open", "USD/UST", "USD/UST")
    assert recovers(venue.template, body["sig"]) == account.address


def test_texts_name_usdc_usdt_and_details_keep_usd_ust(make_client):
    assert (crx.pair_label("USD/UST"), crx.pair_label("USD/MXN")) == ("USDC/USDT", "USD/MXN")
    with pytest.raises(crx.MarketPaused, match="^USDC/USDT is not offered on avax-fuji$") as e:
        make_client(key=False).market("USDC/USDT")
    assert e.value.details == {"pair": "USD/UST"}
