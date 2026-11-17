# Changelog

## 0.4.0

- `trades()` and `rfqs()` take `recover_restore=False`. With `True`, a `conflict` on `after` (a cursor past the head of a restored tape) reads the tape again from the start. The default keeps the 0.3.20 behaviour: the call raises `conflict`.
- Close and takeover signatures show the pair, direction, size, settlement, incoming party and payment terms. A takeover at a benchmark shows its pricing rule and conditional fee. The client checks each displayed term before signing.
- Existing close, answer and takeover calls keep their arguments. The earlier signature format remains supported during the release transition.

## 0.3.21

- A POST that answers `503 service_restarting` is sent again after each `Retry-After` (1 s when absent, 0.5 s at least), for up to 6 s from the first send, with the same body bytes. A signed call signs new headers for each send; a session call sends its token again. A session client's `POST /session` sign-in does the same, for every call, GETs included.
- Once a POST was sent again, any answer other than a 2xx, or no answer, reads as its `503 service_restarting`, with the `details` of each such 503 (for example the `rfq_id` of an open RFQ). Past 6 s that 503 goes back too. Each call reads it as in 0.3.20: `accept()` and `withdraw()` read their status, the close calls wait on the round, and other calls raise `ServerError`.
- The close-round accept, both decline posts and the takeover bid are sent again within their own deadlines. Not sent again: an `eth_sendRawTransaction` on the `/rpc` route (its hash tracks it), and the close posts that repeat on their own (close open, novation, close answer, close sign, takeover sign), sign-in included. Other answers and the GETs of a signed client are unchanged.

## 0.3.20

- `close_trade(trade_id, to=None, limit=None, wait=None)` closes an open trade. When the gateway offers closes with the counterparty and picks its answer, the trade ends for both seats. Else the best quote of another seat, or of the one seat `to` names, takes over this seat's side. `limit=` is the worst close rate the seat takes. The SDK checks the close against the trade before it signs. On a mismatch it raises `RefusedToSign` and signs nothing. The call returns a `crx.Close`.
- `close_requests()` yields each request of a counterparty to close a trade with this seat. `answer_close(request, rate)` answers one. When the gateway picks the answer, the SDK checks the close against the trade and the rate, then signs it.
- `crx.Close` is new: the result of `close_trade()` and `answer_close()`.
- A `GET /markets` that fails with `NetworkError`, `ServerError` or `MarkUnavailable` goes once more after a random pause of 0.5 to 1 s. Before, the pause was 0.5 s. Other GETs keep 0.5 s. Each GET still goes at most twice.
- The install line names `@v0.3.20`.

## 0.3.19

- On testnet, each signed gateway call names its chain. The signed message carries `Chain: 43113` after `Audience: crx-gateway`, and the call sends the header `x-crx-chain: 43113`. The Sandbox gateway refuses a signed call that names no chain (401). On mainnet (chain 1) the signed message and the headers are unchanged.

## 0.3.18

- The test request worker (`python -m crx.test_request_worker`) accepts each test request on its own. A trade with no answer does not stop the accept of another request, and a scan does not wait for a trade status. Test requests from two makers at the same time each get an accept. `accept()` is unchanged: while an earlier accept has no answer, it raises `TradeUnknown`.
- `crx.RelayUnavailable` is removed. It was a second name for `crx.ServiceUnavailable`: catch that class. The SDK no longer reads the gateway code `relay_unavailable`. The gateway sends `service_unavailable`.
- `examples/quickstart.py` is the script of the API Quickstart page. It deposits 100 USDC and requests a quote for 100 USD of USD/MXN. It accepts the quote at 19.5 or lower, reads the positions and withdraws 50 USDC. Before, it deposited 20,000 USDC and traded 25,000 USD of USD/BRL. `python -m crx.quickstart` on testnet is unchanged.

## 0.3.17

- Mainnet needs no RPC of your own. With no `rpc_url=` and no `CRX_RPC`, the client sends its chain calls to `<base URL>/rpc` (`https://api.crxfx.com/rpc`, or the `base_url=` / `CRX_BASE` host) as gateway calls: the signed headers over the exact body bytes (`x-crx-address`, `x-crx-ts`, `x-crx-sig`, and `x-crx-signer` for a delegate), or the session token of a custodian signer. Redirects are not followed. `rpc_url=` or `CRX_RPC` replaces the route and gets no authentication. A `CRX_RPC` that holds `<` or `>` is ignored, with one log line. Testnet is unchanged.
- On the `/rpc` route, any answer other than 200 raises the typed gateway error (`AuthError`, `NotWhitelisted`, `RateLimited`, `ServerError` and others), with `details['method']`. A 429 waits `Retry-After` once (60 s at most) and sends the call again, then raises `RateLimited`. A 5xx to a send is tracked by the transaction hash, as a send with no answer.
- On the `/rpc` route, the pinned chain id, core and domain from `/health` stand: no chain id or core code read. Nonce and receipt reads pause 4 s.
- Each JSON-RPC call carries a random `id`. A refused receipt read after a send raises `SendUnknown` with the transaction hash.

## 0.3.16

- `quote()` and `ask()` need `settles=` (or the older `expiry=`); there is no default. A call with neither raises `BadRequest` that names the accepted forms.
- `settles=` takes an exact UTC time only: a datetime with a timezone, an ISO 8601 time with `Z` or an offset (`"2026-10-24T15:00Z"`), or a Unix time in seconds as an integer (`int` or any `numbers.Integral`, such as a numpy integer) or a float (`1792854000`). The time is sent as given.
- An exact time must be at least the shortest tenor plus 3 minutes from now (the time to open the request), outside the next long close and within the longest tenor. Else `BadRequest` before any request, with `details['next_time']`. A request sent again after a collateral refusal is checked again with the same rule.
- `settles=` no longer takes a tenor (`"1D"`, `"1W"`, `"1M"`), an hour tenor (`"1H"` to `"23H"`) or a date (`"2026-12-18"`): each raises `BadRequest` that names the accepted forms. A bool, or a number of 100,000,000,000 or more (milliseconds or finer), raises `BadRequest`. `expiry=` is unchanged.
- The `tzdata` dependency on Windows is removed: no code reads a time zone database.
- The Solana network is removed: `network="solana"`, `Client(keypair=, check_rpc_url=)`, `CRX_CHECK_RPC`, `bind()`, `bind_state()`, `deposit(keypair=, unsigned=, authority=)`, the errors `SeatStopped`, `SeatBoundOtherPayout`, `BindInProgress` and `BindFailed`, and the `crx-python[solana]` extra. `network="solana"` raises `ConfigError` (unknown network); a removed argument raises `TypeError`. Testnet and mainnet are unchanged.
- On testnet, a `/health` chain id other than 43113 raises `ConfigError` ("not a testnet"). Nothing is signed, as before.
