# Changelog

## Unreleased

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
