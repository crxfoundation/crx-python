# Changelog

## 0.3.16

- `settles=` takes an exact UTC time only: a datetime with a timezone, an ISO 8601 time with `Z` or an offset (`"2026-10-24T15:00Z"`), or a Unix time in seconds, as an int or a float (`1792854000`). The time is sent as given. A time less than the shortest tenor away, in the next long close or past the longest tenor raises `BadRequest` before any request, with `details['next_time']`.
- `settles=` no longer takes a tenor (`"1D"`, `"1W"`, `"1M"`), an hour tenor (`"1H"` to `"23H"`) or a date (`"2026-12-18"`): each raises `BadRequest` that names the accepted forms. A bool, or a number of 100,000,000,000 or more (milliseconds or finer), raises `BadRequest`.
- With no `settles=` and no `expiry=`, the default is unchanged: the pair's fixing one month ahead, on the next day that settles. `expiry=` is unchanged.
- The Solana network is removed: `network="solana"`, `Client(keypair=, check_rpc_url=)`, `CRX_CHECK_RPC`, `bind()`, `bind_state()`, `deposit(keypair=, unsigned=, authority=)`, the errors `SeatStopped`, `SeatBoundOtherPayout`, `BindInProgress` and `BindFailed`, and the `crx-python[solana]` extra. `network="solana"` raises `ConfigError` (unknown network); a removed argument raises `TypeError`. Testnet and mainnet are unchanged.
- On testnet, a `/health` chain id other than 43113 raises `ConfigError` ("not a testnet"). Nothing is signed, as before.

## 0.3.15

- A key file (`key_file=`, `CRX_WALLET_PK_FILE`, a Solana keypair file) that group or other users can open is set to owner-only (0600) when the current user owns it. One line goes to stderr: `set <path> to owner-only (0600)`. A key file of another user, a symlink to one, or a file whose mode does not change is refused as before. The mode check and the read use one open file; the key is read after the check.
- The client reads the `CRX_*` settings (`CRX_WALLET_PK`, `CRX_WALLET_PK_FILE`, `CRX_BASE`, `CRX_RPC`, `CRX_CHECK_RPC`, `CRX_STATE_DIR`, `CRX_ALLOW_MAINNET`) from a `.env` file in the working directory. Lines are `NAME=VALUE`, with optional `export`, `#` comments and single or double quotes; nothing is expanded. A name in the environment wins. When the environment holds `CRX_WALLET_PK` or `CRX_WALLET_PK_FILE`, the file gives neither. A `.env` file with a `CRX_*` line gets the owner-only rule; a `.env` file with none is not changed. The file's values do not go into `os.environ`, and no error or log line carries one.

## 0.3.14

- `quote(settles=)` and `ask(settles=)` take `"1H"`, the shortest settlement: the pair's shortest tenor (`tenor.min_secs`, 1 h on mainnet) plus 3 min, rounded up to the minute. The gateway checks the 1 h minimum on its own clock when the request arrives; the 3 min cover a client clock up to 2 min behind the gateway's and the open. `"2H"` to `"23H"` settle that many hours plus 3 min from now. An hour tenor whose end falls in the pair's next long close (`Market.next_long_close`) moves to its end. A request sent again after a collateral refusal takes a new end.
- `settles=` takes an exact time: `"2026-10-24T15:00Z"`, `"2026-10-24T15:00:00+00:00"`, `"2026-10-24T10:00-05:00"` or an aware datetime. Other offsets convert to UTC. The time is sent as given, never moved. A time with no timezone raises `BadRequest`. A time less than the shortest tenor away, in the next long close or past the longest tenor raises `BadRequest` before any request, with `details['next_time']`.
- Tenors (`"1D"`, `"1W"`, `"1M"`) and dates are unchanged.

## 0.3.13

- `quote()` and `ask()` send the same RFQ request again, with the same `client_rfq_id`, every 15 s while the gateway refuses it with 422 `insufficient_collateral` and `balance().free` covers the amount the refusal names (`details['required']`, else the "does not cover the X USD" figure). A seat at zero collateral gets this refusal after a deposit until the RFQ's collateral check counts the deposit; `/balance` counts it first. The refusal opens no RFQ. New argument `collateral_wait=`, default 600 s. Still refused at the end: the same `InsufficientCollateral`, in the gateway's words. Free collateral below that amount, or any other error, raises at once.
- `send_quote()` posts the same signed quote again on the same refusal for the maker's side, every 15 s until the RFQ's `closes_at`.

## 0.3.12

- `test_request()` sends the request again every 30 s, within `wait`, while the gateway answers 403 `forbidden` "The account has insufficient available collateral for this test request.". A maker that deposits at zero collateral gets this answer until the deposit counts; 0.3.11 raised `AuthError` at once. The refusal opens no request and uses none of the day's one. Still refused at the end: `InsufficientCollateral` with the gateway's sentence, `details['side']` `maker`. Any other 403 raises at once.

## 0.3.11

- `withdraw()` reads `/balance` again every 3 s, for 90 s at most within `wait`, while `withdraw.nonce` is null and `withdraw.nonce_reason` is `settlement_unread` or `unavailable`. For some seconds after each settlement, mainnet serves no nonce; 0.3.10 raised `RefusedToSign` at once. Still no nonce at the end: `RefusedToSign` names the reason, also in `details['nonce_reason']`. Any other reason, or none, raises at once. Nothing is signed without a nonce.

## 0.3.10

- `deposit()` counts a deposit only when the same `/balance` read that names it `credited` and `final` serves `free` at the deposit's amount or more. A deposit can be in `collateral` before `free` holds it: on a new testnet account, 0.3.9 returned `counted` with free collateral 0 for 9 minutes, and the next `quote()` raised `InsufficientCollateral`. A deposit that pays an amount owed counts only when `free` still reaches the amount.

## 0.3.9

- `quote()` and `ask()` take `settles=`: when the trade settles. A tenor (`"1W"`, `"1M"`, `"2M"`), a date (`"2026-12-18"` or a `datetime.date`) or a datetime with a timezone. A tenor or a date settles at the pair's fixing on that date, read from `/markets` `settlement.fixing` (USD/MXN: 12:00 Mexico City). A tenor moves to the next day that settles: Monday to Friday, outside the pair's next long close, at least the shortest tenor (1 h) from now. A date that does not settle raises `BadRequest`; `details['next_date']` is the next date that does. Past the longest tenor (90 days) raises `BadRequest`. `expiry=` still works; pass one of the two.
- Default settlement: `"1M"` at the fixing. A pair whose row serves no fixing keeps the 0.3.8 default (30 days out, off the weekend).
- `settles_at`: the settlement time, UTC, on `Quote`, `Ask`, `Rfq` and `MakerQuote` (the same instant as `expiry`), and on `Position` (the row's `settlement`).
- Windows installs `tzdata` for the fixing zone.
- Requires `certifi>=2026.10.19`. `requests` verifies the gateway's TLS certificate against the certifi bundle; 2026.10.19 removes `emSign ECC Root CA - G3`, whose intermediate keys are public (CVE-2026-111943). Older bundles trust that root.

## 0.3.8

- `withdraw()`: free collateral below the amount (with no amount: under 1 USDC) is read again every 5 s for 120 s at most, within `wait`, and the intent is signed once free collateral covers it; still short at the end raises `InsufficientCollateral` (or `BelowMin`), as before. For about 12 minutes after a trade opens, `/balance` can read free collateral 0 for a moment; 0.3.7 raised at that first read.

## 0.3.7

- `test_request()` sends the request again every 30 s, within `wait`, while the gateway refuses it for collateral not yet counted: `InsufficientCollateral` with `details['side'] == 'maker'`, or `ServerError` with gateway code `collateral_unverified`. Such a request does not count toward the one test request per UTC day. 0.3.6 also sent a taker-side `insufficient_collateral` again; 0.3.7 raises it at once.

## 0.3.6

- `test_request()`: a 422 `insufficient_collateral` or a 503 `collateral_unverified` (collateral the gateway does not count yet) opens no request and uses none of the day's one. The call sends it again every 30 s within the same `wait` as its rate-limit retry (default: 30 minutes on testnet, 15 minutes on mainnet); then that refusal raises. 0.3.5 raised at once.
- Holds the 0.3.5 `deposit()` change and the change above in one release.

## 0.3.5

- `deposit()` counts a deposit once `/balance` `deposit.last` names its tx `credited` and `final`; it no longer waits for `proven`. The gateway reads a deposit `credited` only when the balance it serves holds it, and `final` when the same status holds at the confirmation depth; `proven` (the sealing check at the head) did not arrive within 30 minutes on testnet while free collateral already held the deposit. A deposit `credited` and not final is read again every 5 s.

## 0.3.4

- The default wait is the network's: 30 minutes on testnet, 15 minutes on mainnet and Solana (`NETWORKS[...]["default_wait"]`). It applies to `accept()`, `confirm()`, `deposit()`, `withdraw()` and `test_request()` when the call names no `wait=`; an explicit `wait=` is unchanged. On testnet a deposit took 13 to 17 minutes to count in free collateral and an accept 16.5 minutes to open; 0.3.3 waited 15 minutes.
- Breaking: `Deposit.final` is now `Deposit.counted`: True once the deposit counts in free collateral (credited, proven and final). No alias.
- `test_request()` 404: the docstring states what `details["message"]` holds, the gateway's refusal text (its message, then its error in brackets when they differ).

## 0.3.3

- `deposit()` returns once the deposit counts in free collateral: `/balance` `deposit.last` names its tx and reads `credited`, `proven` and `final`, the state of the `deposit.final` event. `/balance` reads a deposit `credited` minutes before free collateral counts it (10 to 14 minutes on testnet, one settlement cycle on mainnet); 0.3.2 returned at that first credited read, and a quote or accept right after could be refused for collateral. New field `Deposit.final`: True on that return. When `wait` (900 s) ends first: `credited` with `final` False when the deposit reads credited only, `pending` when it does not. `failed` still raises `DepositFailed`. On Solana the read still ends at `credited`.
- The maker quickstart counts a deposit that is not in free collateral by its credit deadline as `deposit_timeout`.

## 0.3.2

- Default wait 900 s for `accept()`, `confirm()`, `deposit()`, `withdraw()` and `test_request()`. Testnet opens took about 10 minutes and a mainnet test trade took 320 s: with 300 s, `accept()` returned `pending` and the next `withdraw()` ran out its `trade_in_flight` retry. `deposit()` returns at the first credited read.
- `confirm(quote, wait=900)`: after the taker's accept, the maker reads the trade until it is `open` or `refused`, at most `wait` s, as `accept()` does. 0.3.1 read it for 30 s on testnet and 90 s on mainnet, then returned `pending`. `timeout` still bounds the wait for the accept. `quote_live()` keeps the 30 s / 90 s read per round; the maker quickstart bounds both waits by its trade deadline.
- `withdraw()`: a 409 `withdraw_in_progress` (another withdraw of the seat, as from another session) queued nothing. The call reads the balance again after 5 s and signs a new intent, while it fits in `wait`; then `WithdrawInProgress` raises. 0.3.1 raised at once.
- `withdraw()` after a returned withdraw that ends `refused` or `returned` raises `EarlierOutcome` once and signs nothing; the next call signs. `paid` or `partial` signs the new withdraw in the same call. 0.3.1 signed the new withdraw in every case.
- A gateway refusal with both a `message` and an `error` that differ reads `<message> (<error>)`, e.g. "Request not accepted. Check the fields. (This account cannot open a test request.)". 0.3.1 showed the message only; the error stays in `details["error"]`.
- README Tasks: the reject snippet requests its own quote first. Rewritten rows say "pays the network fee".

## 0.3.1

- `test_request()` on a network that offers no test requests (HTTP 404) raises `CrxError` with code `test_requests_off`: "test requests are not offered on <chain>". `details["message"]` is the gateway's line. 0.3.0 raised `CrxError` `not_found`.
- `withdraw()`: a balance read that names no free collateral is read again every 5 s within `wait`; still none at the end raises `MarkUnavailable`. `/balance` can name none for a moment after a settlement update; 0.3.0 raised `MarkUnavailable` at once.
- `withdraw()`: `accepted` is in progress, as `sending` and `pending`; `paid`, `partial`, `refused` and `returned` are final. A withdraw that reads `accepted` returns and keeps the hold: no next withdraw signs until it is final. 0.3.0 ended the hold at `accepted`, and the gateway refused the next withdraw with `withdraw_in_progress`.

## 0.3.0

One call per task. The SDK signs, checks amounts and reads the status itself; each refusal raises a typed `crx.CrxError`.

- Breaking: `deposit()` raises `DepositFailed` (`deposit_failed`) on a deposit that reads `failed`. 0.2.10 returned it with status `failed`.
- Breaking: `deposit()` and `accept()` (`trade()`) read the status until it is final for `wait` s at most, default 300. 0.2.10 read it for 30 s on testnet and 90 s on mainnet. `withdraw()` and `test_request()` also take `wait=` (default 300).
- `accept(quote, wait=300)` is the new name of `trade()`. `trade` stays as the same method.
- `reject(quote)` rejects the quote's RFQ on this client: a later `accept()` on it raises `QuoteRejected` (`quote_rejected`) and signs nothing. Nothing is sent; the quote lapses at its `expires_at`.
- `Position` adds `entry_price` (the row's, else `rate`), `mark_price`, `mark_time` (UTC) and `unrealized_pnl` (signed USDC, from your side). A gateway that serves no mark reads None.
- `withdraw()` with no amount withdraws all free collateral, rounded down to 6 decimals. Before a signature it reads `balance()`: no free collateral named raises `MarkUnavailable`, an amount above free collateral `InsufficientCollateral` (`details`: `free`, `requested`), and free collateral under 1 USDC on the no-amount form `BelowMin`.
- `withdraw(wait=300)`: a 409 `trade_in_flight` or 409 `conflict` "the latest settlement is not read yet" sends the same signed intent again every 5 s while it fits in `wait`; then that refusal raises. 0.2.10 raised the `conflict` at once.
- `withdraw()` after a call that returned a withdraw with no final status reads that withdraw every 5 s until it is final, within `wait`, then signs its own in the same call. Still not final at the end raises `WithdrawInProgress` naming the earlier item; nothing signed. 0.2.10 raised `EarlierOutcome` once the earlier withdraw was paid. After a call that raised, the hold is unchanged.
- `test_request(wait=300)`: a `RateLimited` that names its time is sent again after it while it fits in `wait`.
- `quote_live(price, seconds=3600, log=print)` quotes the RFQs that open after the call at the rate `price(rfq)` returns, follows each quote to a trade or its end, drops a quote 10 s past its RFQ's close and drops every live quote on exit. It returns the trades that opened.
- New errors, exported from `crx`: `DepositFailed`, `QuoteRejected`, `TradeInFlight` (`trade_in_flight`), `WithdrawInProgress` (`withdraw_in_progress`).
- The quickstarts and `examples/` use `accept()`, `deposit(wait=)` and the position's entry price and P&L. The README has a Tasks section: one short snippet per task.
- `test_request()` and the test request worker accept the gateway's new test request terms: the request lasts 15 minutes and settlement is one hour after `request_expires_at`. They also accept the earlier terms, three hours and two hours. 0.2.10 refuses the new terms with `BadAnswer`.
- `python -m crx.quickstart`, `python -m crx.quickstart_maker` and `examples/quickstart.py` print one line per step: the step number, its name, the HTTP method and route it calls, and its result. The taker run has six steps, the maker run four.
- The maker quickstart waits up to 15 minutes for the trade to open. On 17 Oct a mainnet test trade took 320 s from quote to open; 0.2.10 waited 300 s.
- The maker quickstart waits up to 30 minutes for deposit credit, counted from the deposit's txs (`deposit(wait=)`). 0.2.10 counted 300 s from the start, so a mainnet deposit (the call alone takes up to 90 s) could time out before credit.

## 0.2.10

- `test_request()` returns a validated 100 USD USD/MXN request for one maker. The request lasts three hours. Each RFQ closes after 120 seconds. Repeating after an unaccepted RFQ expires renews it without changing `Rfq.request_expires_at` or settlement, two hours after that deadline. Renewal does not consume another daily request. `Rfq.reference_rate` provides an example quote price.
- The maker quickstart uses one maker account and deposits the shortfall to 20 USDC, at least the 100 USDC minimum deposit. It waits for credited collateral and an open trade, then reads the trade back from `positions()` until it is final and prints it; a timeout returns an error.
- `python -m crx.test_request_worker` accepts only the dedicated account's marked test requests through the existing acceptance checks and signed trade path. Each scan rereads current status and requires a nonempty `test-request-` owner id. The CLI requires `--enable` and reads its connection from `CRX_RPC`. The existing quote signing ceiling is unchanged.
- The test request worker resumes accepted, pending requests through the idempotent accept route without another trade signature. Recovery survives restart and elapsed deadlines; server status ends recovery. `Client.trade()` retains its at-most-once behavior.

- `trade()` and `withdraw()` post each signed request once. A refusal raises at once and names the time to try again when the gateway names one.
- `trade()`: a 409 `rejected` raises `QuoteExpired` (`gateway_code` `rejected`) at once. 0.2.9 posted the same accept again every 3 s until the RFQ closed.
- `trade()`: on 409 `quote_dropped`, `QuoteDropped` raises and nothing is signed for another quote. `.best` is the best live quote on the same RFQ, or None: pass it to `trade()` to accept it. 0.2.9 accepted `.best` once when its rate was no worse.
- `withdraw()`: a 409 `conflict` ("the latest settlement is not read yet") raises at once with the gateway's line. Nothing is queued: call `withdraw()` again after the time it names. 0.2.9 posted the same intent again for up to 20 minutes.
- Any gateway refusal: `details["time_utc"]` is the UTC time its `message` or `error` names (`17:05 UTC`). `details["retry_after_secs"]` comes from the body's `details`, else the body, else the `Retry-After` header.
- `trade()` refusals keep the gateway's `message`, `outcome` and time in `details`, and the `Retry-After` header too.
- `withdraw()` of the same amount after `WithdrawUnknown` still posts the same signed intent, once per call. The gateway's answer to that post, when it refuses it, is in the next `WithdrawUnknown` as `details["refusal"]`.

## 0.2.9

- Ethereum mainnet (chain 1) pins core `0x0d716cA9Fc2BFD8c66D2AF2fB38b56D08809A8cc`. The signing domain is built from this pin. 0.2.8 pins the former core: from the move, it refuses to sign on Ethereum mainnet (`RefusedToSign`, "core moved"). Update to 0.2.9.
- Testnet (Avalanche Fuji, chain 43113) pins core `0x072000D0EB830F767DC5128ED28139bB731b1521`. 0.2.8 pins the former testnet core: from the move, it refuses to sign on testnet (`RefusedToSign`, "core moved").
- `crx.client.MAINNET_CORE` names the Ethereum mainnet core. A network whose pinned core is not an address signs nothing (`ConfigError`).
- `Trade.provisional` (`trade()`, maker `confirm()`): True while CRX marks the open provisional (`trade_provisional`). No key reads False.
- `Trade.final` and `Position.final`: True once CRX marks the trade final (`trade_final` on the RFQ, `final` on a `/positions` row): no `trade.retracted` can follow. An `open` is not final. A gateway that sends no mark marks no trade final; a missing key or elapsed time never reads final.
- A repeat `trade()` on an accept read `open` or `refused` reads its status again. A failed read keeps the answer. A read of `pending` (the open was taken back) holds other quotes again.
- Docs: a `trade.opened` with no `provisional` key is open; `trade.retracted` can still take it back. `trade.final`, or a `trade.opened` with `final: true`, marks it final.

## 0.2.8

- A retried trade or withdraw call sends once.
- Ethereum: each network pins its chain id and core: testnet (chain 43113) `0xa2F94aA752D4a703028eCfAE8686264dC0928B9c`, mainnet (chain 1) `0x90e32979611dB01CDFbA49C1446995EcB97a26bf`. The signing domain is built from the pin, not from `/health`. A `/health` core other than the pin raises `RefusedToSign` ("core moved"); another chain id raises `ConfigError`. Nothing is signed.
- `Client(core=)` replaces the pinned core. A value that is not an address raises `ConfigError`. On Solana, `core=` raises `ConfigError`.
- `trade()` is at most once. One `trade()` at a time per client. A second `trade()` on an RFQ this client signed an accept for signs nothing: it reads the trade status and returns it. While an earlier accept is not `open` or `refused` (`sending` and `pending` included), `trade()` on another RFQ signs nothing and raises `TradeUnknown`. When that trade opens unread, the next `trade()` raises `EarlierOutcome` once. A final answer is never read again. Past the quote end plus 120 s, a position `positions()` lists `open` is the final answer.
- `trade()`: a 200 to the unsigned template ask raises `TradeUnknown`: the gateway holds an accept on the RFQ already. `mark_unavailable` after the signature keeps the RFQ blocked until its final answer.
- `withdraw()` is at most once. A post with no answer (network, timeout, 5xx other than `service_unavailable`) reads the status; when the item does not show, `WithdrawUnknown` (`withdraw_unknown`) raises. Until the caller reads a final answer, `withdraw()` signs nothing. After a call that raised, the same amount reads the held intent and returns it; when `/balance` does not show it, it posts the same signed intent again, 300 s or more after the last post and not within 300 s of the deadline; only a refusal the gateway gives past its held-item check (409 `withdraw_in_progress` or `conflict`, 422 on the nonce or above free) ends the hold. Any other call raises `WithdrawUnknown` while the held intent has no final answer, then `EarlierOutcome` (`earlier_outcome`) once. A new `Client` holds no earlier trade or withdraw.

## 0.2.7

- Ethereum: a removed account can still deposit to itself. `deposit()` takes the gateway's `depositFor` when its account is this seat's own address and its amount is the deposit's; `depositFor` to any other address is refused (`RefusedToSign`), nothing sent.

## 0.2.6

- On Solana, a seat CRX removed can deposit to itself: `deposit()` takes the gateway's `crx_core.deposit_for` tx when its payer and account are this seat (same row and seat key); any other `deposit_for` is refused, nothing signed. The `seat_stopped` line reads "no new trade; deposit() and withdraw() still work".

## 0.2.5

- Docs: `trades()` and the README say how to read provisional and final trade events. Key trades on `trade_id`; a `trade.opened` with no `provisional` key is the final state; `trade.retracted` removes a provisional open. No code change.

## 0.2.4

- A Solana deposit's "not sent" verdict reads the tx's own blockhash (`isBlockhashValid`, confirmed), not the served `last_valid_block_height`: both RPCs must read it not valid at a slot past the slot where it last read valid, then hold no status for the tx. A blockhash not valid before the send: the tx is not sent, `TxFailed` with `details['reason']` `expired`.
- `sendTransaction` errors -32002, -32003 and -32602 read as not sent. Any other error object: the status reads decide, as for a send with no clear answer.

## 0.2.3

- Ethereum: a tx with no receipt after 300 s raises `SendUnknown` (`send_unknown`), `.tx` its hash, `details['txs']` every hash that can still be mined. It was `TxFailed`. `TxFailed` after a send means the tx reverted on the chain. A receipt with no readable status is read as no receipt.

## 0.2.2

- A Solana deposit sends its tx once. A send with no clear answer (none, not JSON, HTTP 5xx) is never read as not sent: the status reads decide. With no confirmed status at the end of the wait, `deposit()` raises `SendUnknown` (`send_unknown`), `.tx` the signature. `TxFailed` with `details['reason']` `expired` (not sent) only when two RPCs each read their block height past the tx's last valid height and hold no status for it. The second RPC: `check_rpc_url=`, `CRX_CHECK_RPC`, or the row's `https://api.mainnet-beta.solana.com`.
- Any network: an RPC answer with HTTP 5xx raises `BadAnswer`. An Ethereum send with such an answer is watched by its hash and recorded, as a send with no answer.

## 0.2.1

- `network="solana"` pins the mainnet program `A32Z1LwBwyE6UB8SmcF1mwHKQfEhtVQ95s9jfpqDFvWE`. Signing calls on Solana no longer refuse for a missing program.

## 0.2.0

Adds the Solana network. Every Ethereum network behaves as in 0.1.1, except for the first line below.

- On every network, a gateway refusal's `str(e)` is the gateway's `message`; `details['error']` holds its `error`. A body with no `message` gives its `error`, as before.
- `network="solana"`: Solana mainnet at `https://portal.crxfx.com/api`. Off until `allow_mainnet=True` or `CRX_ALLOW_MAINNET=1`; needs an RPC (`rpc_url=` or `CRX_RPC`). Every signing call refuses until the SDK pins the program.
- `Client(keypair=)` takes the Solana wallet: a `solana-keygen` JSON file (mode 600), a list or bytes. The seat key is derived from the wallet's signature of one fixed text, the same key the site derives. Install the extra: `crx-python[solana]`.
- Before the first signature the client checks the cluster, the program, the 3-field domain (no chain id) and the core and vault addresses. A mismatch refuses (`RefusedToSign` or `ConfigError`); nothing is signed.
- `bind()` binds the seat to its wallet (deposit authority and payout) once: the bind fixes the payout account for good. The seat signs; CRX sends and pays the fee. It returns once `GET /bind` reads the seat bound with the three keys of the bind (authority, payout wallet, payout account), each compared as bytes; it reads for 240 s at most. On a seat bound to the same three keys it returns the bind: nothing is signed or posted.
- `bind()` raises `SeatBoundOtherPayout` (`seat_bound_other_payout`) on a seat bound to another payout wallet or payout account, and `CrxError` (`seat_already_bound`) on one bound to this payout and another authority; `details['bound']` holds the chain's keys, `details['filed']` the bind's. A bind with no end after the wait, or filed while another bind of the seat is in progress, raises `BindInProgress` (`bind_in_progress`): it has not failed. A bind that ended with the seat not bound raises `BindFailed` (`bind_failed`). A service that sends no bind raises `ServiceUnavailable`.
- `bind_state()` reads the seat's bind (`GET /bind`): `bound`, `status`, `authority`, `payout_wallet`, `payout_ata`.
- `deposit()` on Solana checks the served tx byte for byte, signs it with the wallet and sends it. `deposit(unsigned=True, authority=)` returns the checked, unsigned tx.
- A Solana deposit that the program refuses on a full intake (program codes 660 and 661) raises `TxFailed`: `no USDC left the wallet; the chain's intake is full until the hourly check; deposit again after it`. `details['reason']` is `intake_full`.
- `withdraw()` on Solana refuses an unbound seat. The payout is the wallet's USDC account fixed at bind.
- On Solana, a seat whose access CRX removed raises `SeatStopped` (`seat_stopped`, a `NotWhitelisted`) from `deposit()`, `bind()`, `ask()`, `quote()` and `trade()`: `CRX removed this seat's access: no new trade and no deposit; withdraw() still works`. `withdraw()`, `balance()` and `positions()` still work.
- On Solana, `quote()` and `ask()` send no RFQ outside the tenor band the pair's `/markets` row serves (`tenor.min_secs`, `tenor.max_secs`): `BadRequest`, `details['limit']` is `tenor`, and the message names when the shortest (or the longest) trade settles now; `details['earliest_expiry']` or `details['latest_expiry']` is that instant, unix ms. The gateway's own tenor refusal (400, `limit: tenor`) raises the same error. A row that serves no band is not checked.
- `keypair=`, `unsigned=` and `authority=` raise `ConfigError` on an Ethereum network.
- On Solana, no SDK error, log or traceback frame holds the wallet secret or the derived seat key.
- On Solana the GET retry, `ServiceUnavailable` and the maker keepalive work as on Ethereum.

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
