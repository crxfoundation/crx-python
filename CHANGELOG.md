# Changelog

## 0.1.1

- `RelayUnavailable` is now `ServiceUnavailable`, code `service_unavailable`. `crx.RelayUnavailable` stays as its alias.
- `SeatCannotSign`, code `seat_cannot_sign`: the address cannot sign trades.

## 0.1.0

- Taker: `quote`, `ask` and `trade` open an RFQ, read the best firm quote and accept it. CRX sends the tx and pays gas.
- Maker: `rfqs`, `send_quote`, `confirm` and `drop_quote` quote the RFQs other seats open. A background thread keeps the seat live.
- Funds: `deposit`, `withdraw`, `balance`, `positions` and the `trades` event tape.
- Viewers: `add_viewer`, `remove_viewer` and `viewers` let up to 5 wallets read a seat.
- Custodian signer: `Client(signer=)` in place of a key.
- A GET is sent once more after a network error, a 5xx or `MarkUnavailable`. POST, PUT and DELETE are sent once.
