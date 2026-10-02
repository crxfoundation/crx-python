"""Viewer grants, and a viewer client that reads another seat (account=)."""

from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

import crx
from crx._http import rest_message

from .test_money import balance_body

EMPTY_BODY = "Body: 0x" + keccak(b"").hex()


def signed(call):
    """(custody, signer, message) from the headers, after the signature recovers to the signer over those
    lines. No ``x-crx-signer`` header: the signer is the custody."""
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


def test_account_signs_custody_owner_signer_key(make_client, session, account):
    owner = Account.create()
    reads(session, owner)
    c = make_client(account=owner.address)  # checksummed in, lower case out
    b = c.balance()
    c.positions(), c.trades()
    assert c.account == b.account == owner.address.lower() and c.address == account.address.lower()
    assert b.free == Decimal("900")
    assert len(session.calls) == 3
    for call in session.calls:
        h = call["headers"]
        assert sorted(h) == ["accept", "x-crx-address", "x-crx-sig", "x-crx-signer", "x-crx-ts"]
        assert (h["x-crx-address"], h["x-crx-signer"]) == (owner.address.lower(), account.address.lower())
        custody, signer, msg = signed(call)
        assert (custody, signer) == (owner.address.lower(), account.address.lower())
        assert f"Custody: {custody}" in msg.splitlines() and f"Signer: {signer}" in msg.splitlines()


def test_account_balance_for_another_seat_refused(make_client, session):
    session.routes[("GET", "/balance")] = balance_body(Account.create())
    with pytest.raises(crx.BadAnswer):
        make_client(account=Account.create().address).balance()


def test_account_equal_to_own_key_is_own_seat(make_client, account):
    c = make_client(account=account.address)
    assert c._gw.custody is None and c.account == c.address


@pytest.mark.parametrize("bad", ["0x12", "not an address", "0x" + "0" * 40, "12" * 20,
                                 "0x5b38Da6a701c568545dCfcB03FcB875f56beddC4", 12])
def test_account_must_be_an_address(make_client, bad):
    with pytest.raises(crx.ConfigError):
        make_client(account=bad)


def test_viewer_client_refuses_seat_calls(make_client, session):
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


def test_remove_viewer(make_client, session, account):
    v, me = Account.create().address.lower(), account.address.lower()
    session.routes[("DELETE", f"/viewers/{v}")] = (204, "")
    assert make_client().remove_viewer(v) is None
    (call,) = session.calls
    custody, signer, msg = signed(call)
    assert (custody, signer, call["raw"]) == (me, me, b"") and msg.endswith(EMPTY_BODY)


def test_viewers(make_client, session, account):
    v, me = Account.create().address.lower(), account.address.lower()
    session.routes[("GET", "/viewers")] = {
        "account": me, "viewers": [{"viewer": v, "granted_by": me, "granted_at": 1_790_000_000_000}], "max": 5}
    (row,) = make_client().viewers()
    assert (row.address, row.granted_by) == (v, me)
    (call,) = session.calls
    custody, signer, msg = signed(call)
    assert (call["method"], call["path"], custody, signer) == ("GET", "/viewers", me, me) and msg.endswith(EMPTY_BODY)


def test_viewer_address_checked_locally(make_client, session):
    with pytest.raises(crx.BadRequest):
        make_client().add_viewer("0x1234")
    assert session.calls == []


@pytest.mark.parametrize("status,code,cls,sdk_code", [
    (409, "viewer_cap", crx.CrxError, "viewer_cap"),
    (400, "viewer_invalid", crx.BadRequest, "bad_request"),
    (403, "viewer_is_maker", crx.BadRequest, "bad_request"),
])
def test_add_viewer_refusals(make_client, session, status, code, cls, sdk_code):
    v = Account.create().address.lower()
    session.routes[("PUT", f"/viewers/{v}")] = (status, {"code": code, "error": "no"})
    with pytest.raises(cls) as e:
        make_client().add_viewer(v)
    assert (e.value.code, e.value.gateway_code, e.value.status) == (sdk_code, code, status)
