"""Typed reads for trade confirmations."""

import crx


TRADE_ID = "0x" + "12" * 32


def record(**extra):
    return {
        "confirmation_id": f"{TRADE_ID}-taker", "trade_id": TRADE_ID, "rfq_id": "0x" + "34" * 32,
        "leg": "T", "status": "issued", "status_text": None, "fixing_election": {"fixing": "BRL_PTAX", "name": "PTAX", "text": "Fixing terms"}, "revision": 1, "content_hash": "sha256:" + "ab" * 32,
        "issued_at": 1795300000000,
        "party": {"role": "taker", "account": "0x" + "56" * 20},
        "counterparty": {"role": "dealer", "legal_name": "CRX Markets and Trading LLC", "lei": None},
        "delivery": {"delivered_at": None, "delivered_via": None}, **extra,
    }


def test_confirmation_reads_typed_record_and_preserves_unknown_fields(make_client, session):
    session.routes[("GET", f"/confirmations/{TRADE_ID}")] = record(status="suspended", status_text="Suspended", future_field={"x": 1}, open_input_labels={"stale": "field"})
    c = make_client()
    got = c.confirmation(TRADE_ID)
    assert isinstance(got, crx.Confirmation)
    assert (got.confirmation_id, got.trade_id, got.party["role"]) == (f"{TRADE_ID}-taker", TRADE_ID, "taker")
    assert got.revision == 1 and got.content_hash == "sha256:" + "ab" * 32
    assert got.leg == "T" and got.counterparty["role"] == "dealer"
    assert got.raw["future_field"] == {"x": 1}
    assert got.raw["open_input_labels"] == {"stale": "field"}
    assert got.status_text == "Suspended"
    assert got.fixing_election == {"fixing": "BRL_PTAX", "name": "PTAX", "text": "Fixing terms"}
    assert not hasattr(got, "suspended_text") and not hasattr(got, "void_text")
    assert not hasattr(got, "open_input_labels")
    assert session.calls[0]["query"] == {"chain": ["avax-fuji"]}


def test_confirmations_returns_cursor_for_the_next_page(make_client, session):
    session.routes[("GET", "/confirmations")] = [
        {"confirmations": [record()], "cursor": 41}, {"confirmations": [record(status="void")], "cursor": 42},
    ]
    c = make_client()
    first = c.confirmations(limit=1)
    second = c.confirmations(after=first.cursor, limit=1)
    assert isinstance(first, crx.ConfirmationPage)
    assert [x.status for x in first] == ["issued"]
    assert [x.status for x in second] == ["void"]
    assert first.cursor == 41 and second.cursor == 42
    assert [call["query"] for call in session.calls] == [
        {"after": ["0"], "limit": ["1"], "chain": ["avax-fuji"]},
        {"after": ["41"], "limit": ["1"], "chain": ["avax-fuji"]},
    ]


def test_confirmation_event_frame_parses_to_typed_confirmation():
    raw = {"type": "trade.confirmation", "seq": 7, "ts": 1795300000000,
           "data": record(future_field="kept"), "frame_extension": True}
    event = crx.Event.from_wire(raw)
    assert event.type == "trade.confirmation" and event.seq == 7
    assert isinstance(event.confirmation, crx.Confirmation)
    assert event.confirmation.raw["future_field"] == "kept"
    assert event.raw["frame_extension"] is True
