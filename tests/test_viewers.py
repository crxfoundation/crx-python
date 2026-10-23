"""Viewer grants, and a viewer client that reads another seat (account=)."""

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

import crx
from crx._http import rest_message

from .test_money import balance_body

EMPTY_BODY = "Body: 0x" + keccak(b"").hex()


def signed(call):
    """Return (custody, signer, message) from the headers once the signature recovers to the signer."""
    h = call["headers"]
    custody = h["x-crx-address"]
    signer = h.get("x-crx-signer", custody)
    assert "x-crx-nonce" not in h
    msg = rest_message(call["method"], call["path"], custody, signer, int(h["x-crx-ts"]), call["raw"])
    assert Account.recover_message(encode_defunct(text=msg), signature=h["x-crx-sig"]).lower() == signer
    return custody, signer, msg


def reads(session, owner):
    session.routes[("GET", "/balance")] = balance_body(owner)
    session.routes[("GET", "/positions")] = {"positions": []}
    session.routes[("GET", "/trades")] = {"trades": [], "seq": 0}


def test_default_signs_as_own_seat(make_client, session, account):
    reads(session, account)
    c = make_client()
    c.balance(), c.positions(), c.trades()
    me = account.address.lower()
    assert c.account == me
    for call in session.calls:
        assert sorted(call["headers"]) == ["accept", "x-crx-address", "x-crx-sig", "x-crx-ts"]
        assert signed(call)[:2] == (me, me)


def test_viewer_client_refuses_seat_calls(make_client, session):
    with pytest.raises(crx.ConfigError):
        make_client(account="0x12")
    c = make_client(account=Account.create().address)
    for call in (lambda: c.quote("USD/MXN", "buy", 25_000), lambda: c.deposit(1), lambda: c.withdraw(1),
                 lambda: c.add_viewer(Account.create().address), lambda: c.viewers()):
        with pytest.raises(crx.ConfigError, match="viewer"):
            call()
    assert session.calls == []


def test_add_viewer(make_client, session, account):
    v, me = Account.create().address.lower(), account.address.lower()
    grant = {"account": me, "viewer": v, "granted_by": me, "granted_at": 1_790_000_000_000}
    session.routes[("PUT", f"/viewers/{v}")] = [(201, grant), (200, grant)]
    c = make_client()
    first, again = c.add_viewer(v.upper().replace("0X", "0x")), c.add_viewer(v)
    assert first == again and first.address == v and first.granted_by == me and first.granted_at.year == 2026
    for call in session.calls:
        custody, signer, msg = signed(call)
        assert (call["method"], call["path"], call["raw"]) == ("PUT", f"/viewers/{v}", b"")
        assert (custody, signer) == (me, me) and msg.endswith(EMPTY_BODY)
        assert "content-type" not in call["headers"]


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
