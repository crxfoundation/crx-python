# CRX Python SDK

Trade FX forwards on CRX from Python.

## Install

```bash
pip install "git+https://github.com/crxfoundation/crx-python@v0.2.7"
```

Python 3.10 or newer.

## Key

```bash
export CRX_WALLET_PK=0x...   # the seat wallet's private key
```

The seat must be onboarded. It needs ETH for gas on mainnet.

Or pass `key_file="seat.key"` (the file must be `chmod 600`).
The SDK never prints or logs the key.

## Quickstart

```bash
CRX_WALLET_PK=0x... python -m crx.quickstart
```

Runs the API Quickstart on Testnet: connect, fund, quote, trade, read, withdraw. Without the key, it asks for it and hides the input. The same steps on Mainnet: `examples/quickstart.py`.

On Solana, follow the steps under [Solana](#solana).

## Hello world

```python
import crx

# 1. Connect. The key comes from CRX_WALLET_PK.
c = crx.Client(network="testnet")
print(c.address)  # 0x7638…fe71

# 2. Fund. Mints any test USDC you lack, deposits. You pay gas.
d = c.deposit(20_000)
print(d.status)  # credited

# 3. Balance.
print(c.balance().free)  # 20000.000000

# 4. Request a quote. Opens an RFQ, returns the best quote.
q = c.quote("USD/BRL", "buy", 25_000)
print(q.pair, q.rate)  # USD/BRL 5.435

# 5. Accept. Signs your confirmation. CRX sends the tx and pays gas.
t = c.trade(q)
print(t.status)  # open

# 6. Your trade.
p = c.positions()[0]
print(p.pair, p.side, p.notional, p.rate, p.status)  # USDBRL buy 25000 5.435 open

# 7. Withdraw.
w = c.withdraw(1_000)
print(w.status)  # accepted
```

## Calls

| Call | Does |
|---|---|
| `health()` | Gateway status. No key needed. |
| `markets()` | Pairs, sessions, limits. No key needed. |
| `next_check()` | The next hourly check, UTC. No key needed. |
| `quote(pair, side, notional)` | Opens an RFQ. Returns the best firm quote after the 10 s window. Accepts nothing. |
| `ask(pair, side, notional)` | Opens an RFQ and returns at once. `.quote()` on the result returns the winning quote after the 10 s window. Accepts nothing. |
| `trade(quote)` | Accepts and opens. CRX sends the tx and pays gas. `status`: `sending`, `open`, `pending` or `refused`. `pending` with no `tx`: no status within the wait. |
| `deposit(amount)` | Approve, then deposit USDC. On testnet, mints the test USDC you lack. `status`: `credited`, `pending` or `failed`. |
| `withdraw(amount)` | Signs a withdraw to your own wallet and sends it in one request. CRX sends the tx and pays gas. `status`: `sending`, `accepted`, `pending`, `paid`, `partial`, `refused` or `returned`. |
| `balance()` | Collateral, free, margin, withdraw state. |
| `positions()` | Open positions. |
| `trades()` | Your event tape: `trade.opened`, `trade.refused`, `trade.settled`, `trade.closed`, `trade.closed_out`, `trade.novated`, `margin.called`, `margin.cured`, and your RFQ events. `market=True` adds every open RFQ a maker seat receives (no owner named). `after=` (the last `Event.seq` you read) reads newer events only. Key trades on `trade_id`. A `trade.opened` with `provisional: true` is followed, once final, by a `trade.opened` with no `provisional` key; that one is the final state. `trade.retracted` removes a provisional open. |
| `add_viewer(addr)` | Lets another wallet read your seat. Up to 5. |
| `remove_viewer(addr)` | Takes that access back. |
| `viewers()` | Wallets that can read your seat. |

`quote()` also takes `expiry=` (datetime, timedelta or unix ms) and `premium_bps=` (the upfront premium, default 0; `trade()` signs only when it is within the market's `max_premium_bps`).

Accept within 120 s of your request: `trade()` posts the accept before `Quote.closes_at`. `Quote.expires_at` is the quote end.

`markets()`: a chain row with no `paused` reads as not paused.

## Maker

A maker seat quotes the RFQs other seats open.

```bash
CRX_WALLET_PK=0x... CRX_TAKER_PK=0x... python -m crx.quickstart_maker
```

Runs the maker Quickstart on Testnet with two accounts: your test taker asks, you quote, sign and open the trade. Without a key, it asks for it and hides the input. Same script: `examples/maker.py`.

The trade reads `open`, or `pending` with a `tx`: accepted, the tx landed. The script exits 0 on both.

Other desks ask on Testnet too, so the script quotes its own test taker's RFQ only: `rfqs(only=ask, wait=10)`.

| Call | Does |
|---|---|
| `rfqs()` | Streams the open RFQs you can quote. A seat with no collateral receives none. `only=` (an `Ask`, or an RFQ id) yields that RFQ alone. `after=` (an `Rfq.seq`) starts after that seq. |
| `rfq(rfq_id)` | One RFQ as your seat reads it. Its taker reads the winning quote only. A maker reads its own quotes only. |
| `send_quote(rfq, rate)` | Signs and posts a binding quote. The gateway takes quotes for the first 10 s of an RFQ only. The taker gets the best quote only. It signs a premium of 0 and the expiry `rfq.expiry` shows, else raises `RefusedToSign`. Pass `premium_bps=` or `expiry=` (a datetime or unix ms, or a timedelta bound) when your rate prices other terms. |
| `confirm(quote)` | Waits for the accept. You sign nothing more. CRX sends the tx and pays gas. `status`: `open`, `sending`, `pending` or `refused`. |
| `drop_quote(quote)` | Ends your binding quote at once. |

Your quote is a signed `Quote`: your trade signature until the RFQ's `quote_expiry_max`, or until you drop it. Its leg id ends in that second. You sign nothing after the accept. A later quote on the same RFQ replaces the earlier one.

The gateway counts a maker live for 15 s after it reads `/trades`. The first maker call (`rfqs`, `send_quote`, `confirm` or `drop_quote`) starts a background thread that reads `/trades` when the client read none for 5 s. `Client(keepalive=)` sets the interval, 1 to 10 s; `None` turns it off. `close()` stops it.

## Custodian signer

Pass `signer=` in place of a key. A signer has:

| Member | Does |
|---|---|
| `address` | The seat wallet. |
| `sign_typed_data(obj)` | Signs the EIP-712 object the SDK built (`eth_signTypedData_v4`). |
| `sign_message(data)` | EIP-191 signature of `data` bytes. The gateway login uses it. |

A signer that signs only a hash (KMS, raw MPC) has `sign_hash(digest)` in place of `sign_typed_data`. The SDK rebuilds the digest from its own typed data, compares it, then asks for the signature.

The custodian must allow EIP-191 text signing. The client logs in by `POST /session`: one signature per 8 h, and again after a gateway restart or revoke. `deposit()`, `send_quote()` and `confirm()` need a local key.

```python
c = crx.Client(signer=my_custodian, network="testnet")
```

## Read another wallet (viewer)

The owner grants read access. The viewer reads with its own key and `account=`.

```python
import crx

owner = crx.Client(network="testnet")  # owner key in CRX_WALLET_PK
owner.add_viewer("0x5b38da6a701c568545dcfcb03fcb875f56beddc4")
print([v.address for v in owner.viewers()])
# ['0x5b38da6a701c568545dcfcb03fcb875f56beddc4']

viewer = crx.Client(key_file="viewer.key", network="testnet", account=owner.address)
print(viewer.balance().free)
# 900.000000
print(len(viewer.positions()))
# 2
```

A viewer reads `balance()`, `positions()` and `trades()` only. Other calls raise `config`.

## Solana

Sign up first at portal.crxfx.com/sign-up with the same wallet. The sign-up runs in a browser wallet: for a keypair file, import the key into a browser wallet first.

Install version 0.2.7 with the extra:

```bash
pip install "crx-python[solana] @ git+https://github.com/crxfoundation/crx-python@v0.2.7"
```

```python
import time

import crx

c = crx.Client(network="solana", keypair="~/.config/solana/id.json", rpc_url="https://…", allow_mainnet=True)
c.bind()               # the seat takes this wallet as authority and payout; bound to it already: signs nothing
c.deposit("1000")      # the wallet signs one Solana tx
while not c.balance().free:
    time.sleep(60)     # a quote needs collateral that an hourly check has credited
q = c.quote("USD/MXN", "buy", 100)
c.trade(q)
c.withdraw("500")      # paid to the wallet's USDC account fixed at bind
```

- The trading (seat) key comes from the wallet: the wallet signs one fixed text, and its keccak256 is the seat key.
  The site makes the same key from the same wallet. Sign that text nowhere but portal.crxfx.com.
- The gateway is `https://portal.crxfx.com/api`. `base_url=` or `CRX_BASE` names another gateway; the text the
  wallet signs stays the same.
- Before the first signature the client checks the cluster (genesis hash), the program, the 3-field domain
  (no chain id) and the core and vault addresses. Any mismatch refuses, nothing signed.
- The CRX program the client signs for is `crx.NETWORKS["solana"]["program_id"]`. While it is `None`, no call
  signs and no wallet is read.
- The keypair file is the `solana-keygen` JSON array, mode 600 (`chmod 600`), as for the Ethereum key file.
- `deposit(unsigned=True, authority="<wallet>")` returns the checked, unsigned tx (base64) for a wallet you
  sign with elsewhere. It refuses when the seat is bound to another wallet.
- A seat opens for deposits after CRX lists it.
- A dealer quotes a size only when its free collateral covers it.
- A bind fixes the seat's payout account for good. `bind_state()` reads the seat's bind: `bound` says the seat
  is bound, not to which keys. Compare `authority`, `payout_wallet` and `payout_ata` with your own, or call
  `bind()`: it compares the three.
- `bind()` returns once the gateway reads the seat bound with the three keys of the bind: the authority, the
  payout wallet and its USDC account. It reads for 240 s at most. A bind with no end by then raises
  `bind_in_progress`: it has not failed, and `bind()` reads its end.
- `bind()` on a seat bound to the same three keys returns the bind and signs nothing. A seat bound to another
  payout raises `seat_bound_other_payout`; to another authority, `seat_already_bound`. `details["bound"]` holds
  the keys the chain holds, `details["filed"]` the ones the bind named.
- A deposit that the program refuses on a full intake raises `tx_failed` with `details["reason"]`
  `intake_full`: no USDC left the wallet. Deposit again after the hourly check.
- `deposit()` sends its tx once. With no confirmed status at the end of its wait it raises `send_unknown`:
  the deposit can be on the chain. `.tx` is its signature: check it on the chain before a new deposit.
  `tx_failed` with `details["reason"]` `expired` means not sent: the tx's own blockhash expired before the
  send, or two RPCs read it expired and then no status for the tx.
  `check_rpc_url=` or `CRX_CHECK_RPC` names the second RPC.
- `quote()` and `ask()` read the pair's tenor band from `/markets` (`tenor.min_secs`, `tenor.max_secs`) and send
  no RFQ outside it. The error is `bad_request` with `details["limit"]` `tenor`. Its message names when the
  shortest (or the longest) trade settles now; `details["earliest_expiry"]` (or `latest_expiry`) is that instant,
  unix ms. The gateway's own tenor refusal reads the same. A pair that serves no band is not checked.

## Errors

Every error is a `crx.CrxError`. Branch on `.code`. Show `str(e)`: on a gateway refusal it is the gateway's `message`.

| Code | Meaning |
|---|---|
| `market_paused` | Pair not live. |
| `below_min`, `above_max` | Notional out of range. |
| `no_quotes` | No maker quoted in time. From `rfqs(only=)`: the RFQ did not reach your seat in time. |
| `rate_out_of_band`, `mark_unavailable` | The gateway declined the accept: the quote's rate is outside the off-market band, or no market price was read. Nothing was reserved or sent. Ask for a new quote. `except crx.Declined` catches both. |
| `service_unavailable` | The service takes no trade now. Nothing was reserved or sent: no trade opened. Accept again later, or ask for a new quote. `except crx.ServerError` catches it too. |
| `seat_cannot_sign` | This address cannot sign trades: it is a smart-contract wallet. Use an EOA or MPC address. Nothing was sent. |
| `rfq_cancelled` | The gateway cancelled the RFQ. `.reason`: `rate_out_of_band` (no quote inside the off-market band) or `mark_unavailable` (no market price). `except crx.NoQuotes` catches it too. |
| `quote_lost` | Your maker quote opened no trade. `reason`: `another_maker`, `expired`, `cancelled`, `dropped` or `timeout`. |
| `quote_format_outdated` | The gateway takes a newer quote format. Update the SDK. Nothing was written. |
| `leg_live` | Your seat holds another live binding quote on the RFQ. Quote again, or `drop_quote(rfq, leg_id=err.leg_id)`. |
| `leg_id_taken` | The quote's leg id is used. Quote again. |
| `quote_fills_full` | The gateway takes no more of your quotes for now. |
| `quote_window_closed` | The quote came after the RFQ's first 10 s. The RFQ takes no more quotes. Quote the next one. |
| `already_accepted` | The taker accepted before your drop. The trade stands: `confirm` it. |
| `unknown_or_ended` | Nothing to drop: the RFQ ended. |
| `quote_expired` | Round ended, or the maker refused. Quote again. |
| `quote_dropped` | The maker dropped the quote. `trade()` takes the best live quote once when its rate is no worse. Else `.best` holds it, or None. |
| `quote_not_yours` | The quote was made for another request or seat. Nothing sent. |
| `rate_limited` | Too many requests. `.retry_after` is the wait in seconds, when sent. |
| `own_round_open` | Your last round is still open. |
| `not_whitelisted` | Onboard the seat first. |
| `seat_stopped` | Solana: CRX removed the seat's access. No new trade. `deposit()` and `withdraw()` still work. `except crx.NotWhitelisted` catches it too. |
| `seat_bound_other_payout` | Solana: the seat is bound to another payout wallet. The bind is permanent. Contact CRX. `details["bound"]` and `details["filed"]` hold the keys. |
| `bind_in_progress` | Solana: a bind of the seat has no end yet: the seat is not bound, and the bind has not failed. `bind()` reads its end. No other bind is taken before it. |
| `bind_failed` | Solana: the bind ended and the seat is not bound. |
| `seat_not_ready` | Onboarding not finished. Wait. |
| `conflict` | The venue cannot take this now. |
| `withdraw_in_progress` | One withdraw at a time. The next opens when this one is paid. |
| `viewer_cap` | 5 viewers already. Remove one first. |
| `insufficient_collateral` | Deposit more. |
| `refused_to_sign` | The gateway served something unexpected. Nothing signed. |
| `bad_answer` | The gateway sent an answer the SDK cannot read, or queued a withdraw other than the one signed. Read `balance()`. |
| `trade_unknown` | The trade may still open. Do not trade again. Read `positions()` after the time the error names. |
| `tx_failed` | A transaction would revert and was not sent, the node refused it, or it reverted on the chain. |
| `send_unknown` | A transaction was sent and its outcome is not known: it can still land. `.tx` is its hash (Ethereum) or signature (Solana). Check it on the chain before you send again. Ethereum: no receipt after 300 s. |
| `network` | The gateway or the RPC did not answer. A GET is sent once more first, also after a 5xx or `mark_unavailable`. |

## Safety

- The SDK rebuilds every digest and transaction before it signs. A mismatch raises `refused_to_sign`.
- A trade carries a readable `summary` line. The SDK builds its own `Trade` from your request and the quote's rate, compares it with the gateway's member by member, checks the domain, the quote end in your leg id and the nonce, and signs its own.
- Every signature leaves with low `s` and `v` 27 or 28. One that does not recover to the seat raises `refused_to_sign`.
- Testnet by default. `network="mainnet"` (Ethereum, chain 1) is off until you pass `allow_mainnet=True` or set `CRX_ALLOW_MAINNET=1`. Its gateway is `https://api.crxfx.com`. It has no default RPC.
- Keep DEBUG logging off in production: urllib3 then logs request paths, and an RPC key can sit in the path.
- A nonce floor lives in `~/.crx-quickstart/`, shared with the quickstart scripts. `CRX_STATE_DIR` moves it.

## Settings

`CRX_BASE` (gateway URL), `CRX_RPC` (chain RPC URL), `CRX_STATE_DIR`, `CRX_ALLOW_MAINNET`.

## Tests

```bash
pip install -e '.[test]'
pytest                      # unit tests, no network
CRX_LIVE=1 pytest -m live   # live, read-only, no transaction
```

## License

Use only to access CRX services. See [LICENSE](LICENSE).
