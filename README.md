# CRX Python SDK

Trade FX forwards on CRX from Python.

## Install

```bash
pip install git+https://github.com/crxfoundation/crx-python
```

Python 3.10 or newer.

## Key

```bash
export CRX_WALLET_PK=0x...   # the seat wallet's private key
```

The seat must be onboarded. It needs test AVAX for gas.

Or pass `key_file="seat.key"` (the file must be `chmod 600`).
The SDK never prints or logs the key.

## Quickstart

```bash
CRX_WALLET_PK=0x... python -m crx.quickstart
```

Runs the API Quickstart on Testnet: connect, fund, quote, trade, read, withdraw. Without the key, it asks for it and hides the input. Same script: `examples/quickstart.py`.

## Hello world

```python
import crx

# 1. Connect. The key comes from CRX_WALLET_PK.
c = crx.Client(network="testnet")
print(c.address)  # 0x7638…fe71

# 2. Fund. Mints any test USDC you lack, deposits. You pay gas.
d = c.deposit(20_000)
print(d.status)  # credited

# 3. Balance. The deposit counts at once.
print(c.balance().free)  # 20000.000000

# 4. Request a quote. Opens an RFQ, returns the best quote.
q = c.quote("USD/BRL", "buy", 100_000)
print(q.pair, q.rate)  # USD/BRL 5.435

# 5. Accept. Signs your confirmation. CRX sends the tx and pays gas.
t = c.trade(q)
print(t.status)  # open

# 6. Your trade.
p = c.positions()[0]
print(p.pair, p.side, p.notional, p.rate, p.status)  # USDBRL buy 100000 5.435 open

# 7. Withdraw. Leaves your balance at once. In your wallet within about 2 hours.
w = c.withdraw(1_000)
print(w.status)  # accepted
```

## Calls

| Call | Does |
|---|---|
| `health()` | Gateway status. No key needed. |
| `markets()` | Pairs, sessions, limits. No key needed. |
| `quote(pair, side, notional)` | Opens an RFQ. Returns the best firm quote. Accepts nothing. |
| `trade(quote)` | Accepts and opens. CRX sends the tx and pays gas. `status`: `sending`, `open`, `pending` or `refused`. |
| `deposit(amount)` | Approve, then deposit USDC. On testnet, mints the test USDC you lack. `status`: `credited`, `pending` or `failed`. |
| `withdraw(amount)` | Signs a withdraw. CRX sends the tx and pays gas. `status`: `sending`, `accepted`, `pending`, `paid`, `partial`, `refused` or `returned`. In your wallet within about 2 hours. |
| `balance()` | Collateral, free, margin, withdraw state. |
| `positions()` | Open positions. |
| `trades()` | Your event tape. `market=True` adds every open RFQ a maker seat receives (no owner named). |
| `add_viewer(addr)` | Lets another wallet read your seat. Up to 5. |
| `remove_viewer(addr)` | Takes that access back. |
| `viewers()` | Wallets that can read your seat. |

`quote()` also takes `expiry=` (datetime, timedelta or unix ms), `im_bps=` and `wait=` (seconds).

## Maker

A maker seat quotes the RFQs other seats open.

```bash
CRX_WALLET_PK=0x... CRX_TAKER_PK=0x... python -m crx.quickstart_maker
```

Runs the maker Quickstart on Testnet with two accounts: your test taker asks, you quote, sign and open the trade. Without a key, it asks for it and hides the input. Same script: `examples/maker.py`.

Other desks ask on Testnet too, so the script quotes its own test taker's RFQ only. It matches `client_rfq_id`, which the gateway serves to an RFQ's own taker alone.

| Call | Does |
|---|---|
| `rfqs()` | Streams the open RFQs you can quote. A seat with no collateral receives none. |
| `rfq(rfq_id)` | One RFQ as your seat reads it. Its taker also reads every desk's quote: `house_rate` is the house desk's. |
| `send_quote(rfq, rate)` | Signs and posts a firm quote. The taker gets the best quote only. Any maker quote outranks the house quote. |
| `confirm(quote)` | Waits for the accept; signs your Side, unless the quote is binding. CRX sends the tx and pays gas. `status`: `open`, `sending`, `pending` or `refused`. |
| `drop_quote(quote)` | Ends your binding quote at once. |

`rfq.sign_mode` names what you sign. `side`: a Leg at the quote, a Side after the accept. `quote`: a binding quote, and nothing after the accept. A binding quote is your trade signature until its `quote_expiry`, or until you drop it. A later quote on the same RFQ replaces the earlier one.

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

## Errors

Every error is a `crx.CrxError`. Branch on `.code`.

| Code | Meaning |
|---|---|
| `market_closed` | The gateway refused: session closed. `details["opens_at"]` is unix ms, when sent. |
| `market_paused` | Pair not live. |
| `below_min`, `above_max` | Notional out of range. |
| `no_quotes` | No maker quoted in time. |
| `quote_lost` | Your maker quote opened no trade. `reason`: `another_maker`, `expired`, `cancelled`, `round_closed`, `dropped` or `timeout`. |
| `leg_live` | Your seat holds another live binding quote on the RFQ. Quote again, or `drop_quote(rfq, leg_id=err.leg_id)`. |
| `leg_id_taken` | The quote's leg id is used. Quote again. |
| `quote_fills_full` | The gateway takes no more of your quotes for now. |
| `already_accepted` | The taker accepted before your drop. The trade stands: `confirm` it. |
| `unknown_or_ended` | Nothing to drop: the RFQ ended. |
| `quote_expired` | Round ended, or the maker refused. Quote again. |
| `quote_dropped` | The maker dropped the quote. `trade()` takes the best live quote once when its rate is no worse. Else `.best` holds it, or None. |
| `quote_not_yours` | The quote was made for another request or seat. Nothing sent. |
| `rate_limited` | Too many requests. `.retry_after` is the wait in seconds, when sent. |
| `own_round_open` | Your last round is still open. |
| `not_whitelisted` | Onboard the seat first. |
| `seat_not_ready` | Onboarding not finished. Wait. |
| `conflict` | The venue cannot take this now. Retry later. |
| `withdraw_in_progress` | One withdraw at a time. The next opens when this one is paid. |
| `viewer_cap` | 5 viewers already. Remove one first. |
| `insufficient_collateral` | Deposit more. |
| `refused_to_sign` | The gateway served something unexpected. Nothing signed. |
| `trade_unknown` | The trade may still open. Do not trade again. Read `positions()` after the time the error names. |
| `tx_failed` | A transaction would revert, or reverted. |
| `network` | The gateway or the RPC did not answer. |

## Safety

- The SDK rebuilds every digest and transaction before it signs. A mismatch raises `refused_to_sign`.
- Testnet by default. `network="mainnet"` (Ethereum, chain 1) is off until you pass `allow_mainnet=True` or set `CRX_ALLOW_MAINNET=1`. It has no default URLs.
- Keep DEBUG logging off in production: urllib3 then logs request paths, and an RPC key can sit in the path.
- A Side nonce floor lives in `~/.crx-quickstart/`, shared with the quickstart scripts. `CRX_STATE_DIR` moves it.

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
