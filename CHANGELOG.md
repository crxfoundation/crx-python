# Changelog

## 0.6.1

The quickstart's end date is now relative: 30 days ahead, a weekend rolls to Monday, at 18:00 UTC.

## 0.6.0

The SDK speaks the gateway's v5 format. Version 0.5.0 and older cannot trade on it.

### Taker

- `trade()` reads `trade_template`, which holds `typed_data` only: the readable `Trade`. The SDK builds its own `Trade` from your request and the quote's rate, compares each member with the served one, computes the digest itself and signs its own typed data. Any difference raises `RefusedToSign`. Nothing is signed.
- Before it signs, the SDK checks the domain (the chain id and core `/health` serves), the type, the premium against the market's `max_premium_bps`, the nonce (at most 24 h ahead, above the last one this machine signed) and the quote end. The last 8 bytes of your leg id must equal the RFQ's `quote_expiry_max`.
- A 409 `trade_stale` carries a fresh template in `details.trade_template`. The SDK checks and signs it in turn.
- The accept body is `{quote_id, sig}`.
- `trade()` posts the accept before `Quote.closes_at`, the RFQ's end, 120 s after the request. `Quote.expires_at` is the quote end: it is not the accept deadline.
- `quote()` sends no `wait`: the gateway answers after the 10 s window, with the winner. `ask()` sends `wait: false`: the gateway answers at once.
- `quote()` and `ask()` take `premium_bps=` (default 0). `im_bps=` is gone.
- An accept the gateway declines raises `Declined`: `RateOutOfBand`, `MarkUnavailable` or `PositionMatured`. An RFQ the gateway cancels raises `RfqCancelled` (a `NoQuotes`) with its `reason`.
- Only a live `quoted` row wins. A row past `expires_at`, dropped or declined never wins.

### Maker

- `send_quote()` signs one binding `Quote`: your half, a fresh salt and the RFQ's `taker_ref`. It has no `instrumentId`, `quoteExpiry`, `wrapsHash` or `imBps`. You sign nothing after the accept.
- The body is `rate`, `leg_id`, `salt` and `sig`, plus `client_quote_id` when you set one.
- The leg id is 24 random bytes, then the RFQ's `quote_expiry_max` as 8 bytes. A leg id that ends in another second is refused before a signature exists.
- The Quote nonce is the big-endian u64 of leg id bytes 16 to 23. The body sends no nonce.
- `send_quote(expires_in=)` is gone.
- A 400 `quote_format_outdated` raises `QuoteFormatOutdated`.

### Login

- Each signed call signs 8 lines: `CRX-REST-LOGIN`, audience, method, path, custody, signer, timestamp (unix ms), body hash. There is no nonce line.
- Headers: `x-crx-address`, `x-crx-ts` and `x-crx-sig`. A key that signs for another account (`account=`) also sends `x-crx-signer`. No `x-crx-nonce`.
- Timestamps are unique per process: max(now, the last one + 1) ms. The gateway refuses a repeated call (401).

### Custodian signer

- `Client(signer=)` takes a custodian in place of a key. It signs the typed data the SDK builds, and EIP-191 for the login. A hash-only signer gets the digest after the SDK rebuilds it from its own typed data.
- The client logs in by `POST /session`: one login signature per 8 h.

### Markets

- `markets()` and `market()` read the one-chain `/markets` reply (no `chains[]`, the premium cap on the pair row) and the earlier shape.

### Reads

- `markets()`: a chain row with no `paused` reads as not paused. `Market.max_premium_bps` is the chain's premium cap.
- `Rfq.closes_at` is the end of the quote window. `Rfq.quote_expiry_max` is the latest quote end. `Rfq.opened_at`, `Rfq.quote_expiry`, `Rfq.im_bps`, `Rfq.sign_mode` and `Rfq.house_rate` are gone.
- `Quote.house` is gone. A taker reads the winning quote only.
- `trades()` carries `trade.closed`, `margin.called` and `margin.cured`. `rfq.accepted` names `rfq_id`, `quote_id` and `client_quote_id` only.

### Withdraw

- The `WithdrawIntent` is `account`, `amount`, `nonce`, `deadline`. It has no recipient: the chain pays the account.
- A custodian signer can sign the withdraw: the SDK hands it the intent as typed data.
