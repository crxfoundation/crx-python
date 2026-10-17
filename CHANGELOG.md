# Changelog

## Unreleased

- `test_request()` returns a validated 100 USD USD/MXN request for one maker. The quote window lasts three hours; settlement is five hours after creation. `Rfq.reference_rate` provides an example quote price.
- The maker quickstart uses one maker account and deposits only the shortfall to 20 USDC. It waits for credited collateral and an open trade; a timeout returns an error.
- `python -m crx.test_request_worker` accepts only the dedicated account's marked test requests through the existing acceptance checks and signed trade path. Each scan rereads current status.

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
