"""Viewer grants, and a viewer client that reads another seat (account=)."""

import pytest
from eth_account import Account

import crx

from .conftest import WEEK  # noqa: F401


def test_viewer_client_refuses_seat_calls(make_client, session):
    with pytest.raises(crx.ConfigError):
        make_client(account="0x12")
    c = make_client(account=Account.create().address)
    for call in (lambda: c.quote("USD/MXN", "buy", 25_000, settles=WEEK), lambda: c.deposit(1), lambda: c.withdraw(1),
                 lambda: c.add_viewer(Account.create().address), lambda: c.viewers()):
        with pytest.raises(crx.ConfigError, match="viewer"):
            call()
    assert session.calls == []


def test_viewers_and_remove_viewer(make_client, session, account):
    v, me = Account.create().address.lower(), account.address.lower()
    grant = {"account": me, "viewer": v, "granted_by": me, "granted_at": 1_790_000_000_000}
    session.routes[("GET", "/viewers")] = [{"viewers": [grant]}, {"viewers": None}]
    session.routes[("DELETE", f"/viewers/{v}")] = (204, "")
    c = make_client()
    assert [(x.address, x.granted_by) for x in c.viewers()] == [(v, me)]
    with pytest.raises(crx.BadAnswer, match="lists no viewers"):
        c.viewers()
    with pytest.raises(crx.BadRequest, match="viewer is not a wallet address"):
        c.remove_viewer("0x12")
    assert c.remove_viewer(v) is None
    assert [(call["method"], call["path"]) for call in session.calls] == [("GET", "/viewers")] * 2 + [
        ("DELETE", f"/viewers/{v}")]
