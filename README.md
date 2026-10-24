# CRX Python SDK

Trade FX forwards on CRX from Python.

## Install

```bash
pip install "git+https://github.com/crxfoundation/crx-python@v0.3.17"
```

Python 3.10 or newer.

Set `CRX_WALLET_PK` to the private key of an onboarded seat wallet. To read the key from a file, pass `key_file="seat.key"` or set `CRX_WALLET_PK_FILE`. When other users can open the file and it is yours, the SDK sets it to owner-only (0600) and prints `set seat.key to owner-only (0600)`. It refuses a key file of another user, or one whose mode does not change. The SDK never prints or logs the key. On mainnet, the wallet needs ETH for the network fee of a deposit.

Mainnet needs no RPC of your own: the client sends its chain calls to the gateway's `/rpc` route, signed with the seat key. `CRX_RPC` is optional and replaces that route with your own node.

The client also reads the `CRX_*` settings from a `.env` file in the working directory:

```bash
# .env
CRX_WALLET_PK_FILE=seat.key
```

One `NAME=VALUE` per line; `#` starts a comment; quotes are optional. Nothing is expanded. A name set in the environment wins over the file. A `.env` file with a `CRX_*` line gets the same owner-only rule as a key file.

## Quickstart

```bash
CRX_WALLET_PK=0x... python -m crx.quickstart
```

The script runs the API Quickstart on testnet: deposit, request a quote, accept, read positions and margin, withdraw. With no key set, it asks for the key and hides the input. `examples/quickstart.py` runs the same steps on mainnet. Each step prints the route it calls and its result:

```text
15:40:18 0x656e3a8c4f1d2b07e9a65c3d4b8f2e1a90c76bed
15:40:42 1/6 Deposit 20000 USDC POST /deposit              -> credited, free 20000.000000
15:40:52 2/6 Request a quote    POST /rfqs                 -> USD/BRL 5.2511, rfq 0x44fd...42f8
15:41:10 3/6 Accept             POST /rfqs/{rfq_id}/accept -> open
15:41:10 4/6 Positions          GET  /positions            -> USD/BRL buy 25000 at 5.2511, open, P&L -1.204512
15:41:10 5/6 Margin (IM)        GET  /balance              -> IM 3059.657629
15:41:34 6/6 Withdraw 1000 USDC POST /withdraw             -> accepted
```

## Tasks

Each call signs, checks amounts and reads the status itself. A refusal raises a `crx.CrxError` with a `code` ([Errors](#errors)).

```python
import time

import crx

c = crx.Client(network="mainnet", allow_mainnet=True)  # reads CRX_WALLET_PK

d = c.deposit(100)                                 # USDC from the seat wallet
print(d.status, c.balance().free)

week = int(time.time()) + 7 * 86_400              # Unix seconds, UTC
q = c.quote("USD/MXN", "buy", 100, settles=week)   # best firm quote, nothing accepted
print(q.pair, q.side, q.notional, q.rate, q.settles_at, q.closes_at)
if q.rate <= 19.5:                                 # the most MXN to pay for 1 USD
    t = c.accept(q)                                # accept before q.closes_at
    print(t.status)
else:
    c.reject(q)

for p in c.positions():
    print(p.pair, p.side, p.notional, p.entry_price, p.settles_at, p.unrealized_pnl)
w = c.withdraw(50)                                 # c.withdraw() withdraws all free collateral
print(w.amount, w.status)
```

`network` is `"testnet"` (default, Avalanche Fuji) or `"mainnet"` (Ethereum). The wallet pays the network fee of a deposit, and CRX sends every other transaction and pays its fee. `unrealized_pnl` is in USDC, from the seat's side.

## Calls

`wait=` on `accept()`, `deposit()`, `withdraw()`, `confirm()` and `test_request()` caps how long the call waits for its result, in seconds. Default: 1800 on testnet, 900 on mainnet.

| Call | Does |
|---|---|
| `health()` | Reads the gateway status with no key. |
| `markets()` | Lists each pair with its session, pause state, notional limits and premium cap, with no key. |
| `market(pair)` | Returns one pair, or raises `market_paused` when this network does not offer it. |
| `next_check()` | Returns the time of the next hourly check, UTC, with no request. |
| `quote(pair, side, notional, settles=)` | Opens an RFQ and returns the best firm quote after the 10 s window. |
| `ask(pair, side, notional, settles=)` | Opens an RFQ and returns at once, and `.quote()` on the result returns the winner after the 10 s window. |
| `accept(quote)`, `trade(quote)` | Signs the trade, posts it once and waits up to `wait` for `open` or `refused`. |
| `reject(quote)` | Sends nothing, and makes a later `accept()` on the quote's RFQ raise `quote_rejected`. |
| `deposit(amount)` | Deposits USDC and waits up to `wait` for it to count in free collateral. On testnet, it first mints the test USDC the wallet lacks. |
| `withdraw(amount=None)` | Withdraws USDC to the seat wallet, or all free collateral with no amount. |
| `balance()` | Returns collateral, free collateral, margin and withdraw state. |
| `positions()` | Lists open positions with entry price, mark price, mark time and unrealized P&L. The mark and the P&L are `None` with no mark. |
| `trades(after=0, market=False)` | Returns the event tape of the seat, oldest first. |
| `add_viewer(addr)`, `remove_viewer(addr)`, `viewers()` | Grants, takes back and lists read access to the seat, for up to 5 wallets. |

`quote()` and `ask()` take the same terms:

| Argument | Values |
|---|---|
| `side` | `"buy"` or `"sell"`, the seat's side in the base currency. |
| `notional` | Base currency amount, at most 6 decimals, from `Market.min_notional` to `Market.max_notional`. |
| `settles` | The exact UTC time the trade settles. See the forms below. No default. |
| `expiry` | The older form of `settles`: a datetime, a timedelta or unix ms. Pass one of the two. |
| `premium_bps` | Upfront premium, -10000 to 10000, default 0. Above 0 the taker pays. |
| `collateral_wait` | Waits up to this many seconds, default 600, for a new deposit to count. |
| `client_rfq_id` | A caller id for the RFQ, 1 to 128 printable ASCII characters. |

- `settles` takes an exact UTC time: a datetime with a timezone, an ISO 8601 time with `Z` or an offset (`"2026-10-24T15:00Z"`, `"2026-10-24T10:00-05:00"`), or a Unix time in seconds (`1792854000`; an int, a numpy integer or a float).
- The time is sent as given, never moved. It must be at least the pair's shortest tenor (`tenor.min_secs`, 1 h on mainnet) plus 3 minutes from now, outside the next long close and within the longest tenor. Else `bad_request` names the next time that settles in `details["next_time"]`. A request sent again after a collateral refusal is checked again.
- A missing `settles` (with no `expiry`) raises `bad_request`. So does any other value: a tenor (`"1M"`), an hour tenor (`"1H"`), a date (`"2026-12-18"`), a time with no timezone, a bool, or a number in milliseconds.

`Quote.settles_at` is the settlement time, `Quote.expires_at` the quote end, and `Quote.closes_at` the last time to accept (120 s after the request). `accept()` signs only a premium within the market's `max_premium_bps`.

`Trade.status` is `open` or `refused`, or `sending` or `pending` when `wait` ends first. `Deposit.status` is `credited` or `pending`, and `Deposit.counted` is true once the deposit counts in free collateral. `Withdraw.status` is `sending`, `pending` or `accepted` while in progress, and `paid`, `partial`, `refused` or `returned` when final.

A chain reorg can still take back an `open` trade. `Trade.provisional` is true while CRX marks the open provisional. `final` on a `Trade` or `Position` turns true once CRX marks the trade final, and no `trade.retracted` follows.

`trades()` returns `trade.opened`, `trade.refused`, `trade.settled`, `trade.closed`, `trade.closed_out`, `trade.novated`, `trade.retracted`, `trade.final`, `margin.called`, `margin.cured` and the seat's RFQ events. Key trades on `trade_id`. A later `trade.opened` with no `provisional` key replaces a provisional one. `trade.final`, or a `trade.opened` with `final: true`, marks the trade final. `after=` takes the last `Event.seq` read and returns newer events only. `market=True` adds every open RFQ a maker seat receives, with no owner named.

## Maker

A maker seat quotes the RFQs other seats open.

```bash
python -m crx.quickstart_maker
```

Set `CRX_WALLET_PK` or `CRX_WALLET_PK_FILE`. The script uses one approved maker account on Ethereum mainnet at `https://api.crxfx.com`, and asks for a missing key with hidden input. Under 20 USDC of free collateral, it deposits the shortfall, at least the 100 USDC minimum deposit. It then calls `test_request()`, quotes the returned `reference_rate` and reads the trade from `positions()` until it is final:

```text
19:22:22 0x7e57de5c4a0c1f3b9e2d58a6c07b41e93f6d0a01
19:27:48 1/4 Deposit        POST /deposit              -> 100 USDC, free 100.000000
19:27:49 2/4 Test request   POST /test-requests        -> USD/MXN sell 100, rfq 0x44fd...42f8
19:27:50 3/4 Quote          POST /rfqs/{rfq_id}/quotes -> quoted 18.493143
19:45:25 4/4 Final position GET  /positions            -> USD/MXN sell 100 at 18.493143, open, final
```

The script exits with success only when the trade is `open` and final. A deposit, trade or finality timeout fails it. `examples/maker.py` runs the same flow.

| Call | Does |
|---|---|
| `test_request()` | Requests a 100 USD USD/MXN test RFQ for this maker and returns an `Rfq` with `reference_rate`. |
| `rfqs()` | Yields the open RFQs the seat can quote, as they arrive. |
| `rfq(rfq_id)` | Returns one RFQ, with the winning quote for its taker or the maker's own quotes. |
| `send_quote(rfq, rate)` | Signs and posts a binding quote, and returns a `MakerQuote`. |
| `confirm(quote, timeout=None)` | Waits for the accept up to `timeout` (default: the quote window), then up to `wait` for the trade. |
| `drop_quote(quote)` | Ends the binding quote at once. |
| `quote_live(price, seconds=3600)` | Quotes each new RFQ at `price(rfq)`, drops every live quote on exit and returns the trades that opened. |

```python
c = crx.Client(network="mainnet", allow_mainnet=True)
rfq = c.test_request()
mq = c.send_quote(rfq, rfq.reference_rate)   # binds the seat until rfq.quote_expiry_max
print(c.confirm(mq).status)
c.quote_live(lambda rfq: "18.49" if rfq.pair == "USD/MXN" else None, seconds=600)
```

`test_request()`: a request lasts 15 minutes. Each RFQ closes after 120 seconds and settles one hour after `request_expires_at`. A repeat call returns the active or accepted RFQ, or renews an expired one with the same deadlines, except in the last minute. Each maker starts at most one request per UTC day, within ten globally. `wait` covers a rate limit and a new deposit that does not count yet. On a network without test requests, the code is `test_requests_off`.

`rfqs()`: a seat with no collateral receives no RFQ. `only=` (an `Ask` or an RFQ id) yields that RFQ alone. `after=` (an `Rfq.seq`) starts after that RFQ. The stream ends after `wait=` seconds or when `stop=` is set, and by default never ends.

`send_quote()`: normal RFQs take quotes for 10 seconds. The taker gets the best quote only. The SDK signs a premium of 0 and the settlement in `rfq.expiry`. Pass `premium_bps=` or `expiry=` (a datetime, unix ms or a timedelta bound) when the rate prices other terms. The call waits until the RFQ's `closes_at` for a new deposit to count. The quote binds the seat until the RFQ's `quote_expiry_max` or a `drop_quote()`. Nothing is signed after the accept. A later quote on the same RFQ replaces the earlier one.

`quote_live()`: `price(rfq)` returns a rate with at most 6 decimals, or `None` to pass. `log=` takes one line per event (default `print`).

The gateway counts a maker live for 15 s after it reads `/trades`. The first maker call starts a background thread that reads `/trades` when the client has read none for 5 s. `Client(keepalive=)` sets the interval, 1 to 10 s, and `None` turns it off. `close()` stops it.

`python -m crx.test_request_worker` accepts dedicated test requests. It requires `--enable`, `--key-file` and `--state-dir`, sends its chain calls to the gateway's `/rpc` route or to `CRX_RPC` when set, and does not renew RFQs.

## Custodian signer

Pass `signer=` in place of a key: `crx.Client(signer=my_custodian, network="testnet")`. The signer has these members:

| Member | Does |
|---|---|
| `address` | The seat wallet. |
| `sign_typed_data(obj)` | Signs the EIP-712 object the SDK built (`eth_signTypedData_v4`). |
| `sign_message(data)` | Signs `data` bytes with EIP-191, for the login. |
| `sign_hash(digest)` | Replaces `sign_typed_data` on a signer that signs only a hash. The SDK first rebuilds and compares the digest. |

The custodian must allow EIP-191 text signing. The client logs in with one signature per 8 h, and again after a restart or revoke on the CRX side. `deposit()`, `send_quote()` and `confirm()` need a local key.

## Read another wallet (viewer)

The owner grants read access. The viewer passes `account=` with its own key, and can call `balance()`, `positions()` and `trades()` only. Other calls raise `config`.

```python
owner = crx.Client(network="testnet")  # owner key in CRX_WALLET_PK
owner.add_viewer("0x5b38da6a701c568545dcfcb03fcb875f56beddc4")
viewer = crx.Client(key_file="viewer.key", network="testnet", account=owner.address)
print(viewer.balance().free)  # 900.000000
```

## Errors

Every error is a `crx.CrxError`. Branch on `.code`. Show `str(e)`: on a gateway refusal it is the gateway's `message`. When the gateway names a time to call again, `details["retry_after_secs"]` holds the wait in seconds and `details["time_utc"]` the UTC time (`17:05 UTC`). `except crx.NoQuotes` also catches `rfq_cancelled`, `except crx.Declined` catches `rate_out_of_band` and `mark_unavailable`, and `except crx.ServerError` catches `service_unavailable`.

| Code | What to do |
|---|---|
| `config` | Fix the client setup the message names: key, network, `allow_mainnet` or RPC. |
| `bad_request` | Fix the argument the message names. |
| `not_whitelisted`, `seat_not_ready` | Finish onboarding the seat. |
| `seat_cannot_sign` | Use an EOA or MPC address. A contract wallet cannot sign trades. |
| `market_paused` | Wait until `markets()` shows the pair live. |
| `below_min`, `above_max` | Change the notional. From `withdraw()` with no amount, free collateral is under 1 USDC. |
| `insufficient_collateral` | Deposit more, or withdraw at most `details["free"]` when `withdraw()` raised it. |
| `no_quotes`, `rfq_cancelled`, `quote_expired`, `quote_rejected`, `quote_not_yours`, `rate_out_of_band` | Request a new quote. `rfq_cancelled.reason` names why. |
| `mark_unavailable` | Request a new quote, or call `withdraw()` again later. |
| `service_unavailable` | Call again later, or request a new quote. |
| `own_round_open` | Wait until the seat's last round closes. |
| `quote_dropped` | Pass `.best`, the best live quote on the same RFQ, to `accept()` when it is not `None`. |
| `trade_unknown` | Do not trade again. Read `positions()` after the time the message names. |
| `earlier_outcome` | Read the earlier call's answer in `details`, then call again. |
| `quote_lost`, `quote_window_closed`, `leg_id_taken` | Quote again, or quote the next RFQ. `quote_lost.reason` is `another_maker`, `expired`, `cancelled`, `dropped` or `timeout`. |
| `leg_live` | Quote again, or call `drop_quote(rfq, leg_id=err.leg_id)`. |
| `quote_format_outdated` | Update the SDK. |
| `network`, `conflict`, `quote_fills_full`, `rate_limited` | Call again later. `rate_limited.retry_after` is the wait in seconds, when sent. |
| `already_accepted` | The taker accepted before the drop. Call `confirm()`. |
| `unknown_or_ended` | The RFQ ended. Nothing to drop. |
| `deposit_failed`, `bad_answer` | Read `balance()` before the next deposit or call. |
| `withdraw_in_progress`, `trade_in_flight` | Withdraw again once the earlier withdraw is paid, or the opening trade has an answer. |
| `withdraw_unknown` | Call `withdraw()` with the same amount to read it. |
| `viewer_cap` | Remove one of the 5 viewers first. |
| `refused_to_sign` | Read the message. The SDK signed nothing. |
| `tx_failed` | Read the message. |
| `send_unknown` | The transaction can still land. Check `.tx` on the chain before a new send. No receipt after 300 s. |

## Safety

- The SDK rebuilds every digest and transaction before it signs. A mismatch, or a signature that does not recover to the seat, raises `refused_to_sign`. Every signature leaves with low `s` and `v` 27 or 28.
- A trade carries a readable `summary` line. The SDK builds its own `Trade` from the request and the quote's rate, and compares it with the served one member by member. It checks the domain, the quote end and the nonce, and signs its own.
- Each network pins its chain id and core contract. Testnet (Avalanche Fuji, chain 43113): `0x072000D0EB830F767DC5128ED28139bB731b1521`. Mainnet (chain 1): `0x0d716cA9Fc2BFD8c66D2AF2fB38b56D08809A8cc`. The signing domain is built from the pin.
- When `/health` names another chain id, core or domain, each signing call raises `refused_to_sign` or `config` and signs nothing. `core=` replaces the pinned core: pass only an address that CRX publishes.
- Keep DEBUG logging off in production. At DEBUG, urllib3 logs request paths, and a path can hold an RPC key.

## Settings

| Argument | Environment | Default |
|---|---|---|
| `allow_mainnet=True` | `CRX_ALLOW_MAINNET=1` | Off. Mainnet needs it. |
| `rpc_url=` | `CRX_RPC` | Optional. Testnet: a public RPC. Mainnet: the gateway's `/rpc` route at the base URL, each call signed with the seat key. An RPC set here gets no signed headers. |
| `base_url=` | `CRX_BASE` | Testnet: `https://api.sandbox.crxfx.com`. Mainnet: `https://api.crxfx.com`. |
| `state_dir=` | `CRX_STATE_DIR` | `~/.crx-quickstart/`, which holds the nonce floor and is shared with the quickstart scripts. |

## Tests

```bash
pip install -e '.[test]'
pytest                      # unit tests, no network
```

## License

Use only to access CRX services. See [LICENSE](LICENSE).
