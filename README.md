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

## Hello world

```python
import crx

c = crx.Client(network="fuji")

try:
    print(c.balance().free)
    q = c.quote("USD/MXN", "buy", 25_000)  # opens an RFQ, returns the best quote
    print(q.rate)
    t = c.trade(q)                         # accepts, signs, sends the arm tx
    print(t.status, t.tx)
except crx.MarketClosed as e:
    print("closed, opens at", e.details["opens_at"])
except crx.CrxError as e:
    print(e.code, e)
```

## Calls

| Call | Does |
|---|---|
| `health()` | Gateway status. No key needed. |
| `markets()` | Pairs, sessions, limits. No key needed. |
| `quote(pair, side, notional)` | Opens an RFQ. Returns the best firm quote. Accepts nothing. |
| `trade(quote)` | Accepts and binds. You pay gas. The next hourly fold opens the position. |
| `deposit(amount)` | Approve, then deposit USDC. On testnet, mints the test USDC you lack. |
| `withdraw(amount)` | Signs and arms a withdraw. Paid after the next fold and crank. |
| `balance()` | Collateral, free, margin, withdraw state. |
| `positions()` | Open positions. |
| `trades()` | Your event tape. |

`quote()` also takes `expiry=` (datetime, timedelta or unix ms), `im_bps=` and `wait=` (seconds).

## Errors

Every error is a `crx.CrxError`. Branch on `.code`.

| Code | Meaning |
|---|---|
| `market_closed` | Session closed. `details["opens_at"]` is unix ms. |
| `market_paused` | Pair not live. |
| `below_min`, `above_max` | Notional out of range. |
| `no_quotes` | No maker quoted in time. |
| `quote_expired` | Round ended, or the maker refused. Quote again. |
| `own_round_open` | Your last round is still open. |
| `not_whitelisted` | Onboard the seat first. |
| `seat_not_ready` | Onboarding not finished. Wait. |
| `conflict` | A withdraw is already live or unpaid. Wait for the crank. |
| `insufficient_collateral` | Deposit more. |
| `refused_to_sign` | The gateway served something unexpected. Nothing signed. |
| `trade_unknown` | The arm may be on chain. Do not trade again. Read `positions()` after the next fold. |
| `tx_failed` | A transaction would revert, or reverted. |
| `network` | The gateway or the RPC did not answer. |

## Safety

- The SDK rebuilds every digest and transaction before it signs. A mismatch raises `refused_to_sign`.
- Testnets only for now.
- Keep DEBUG logging off in production: urllib3 then logs request paths, and an RPC key can sit in the path.
- A Side nonce floor lives in `~/.crx-quickstart/`, shared with the quickstart scripts. `CRX_STATE_DIR` moves it.

## Settings

`CRX_BASE` (gateway URL), `CRX_RPC` (chain RPC URL), `CRX_STATE_DIR`.

## Tests

```bash
pip install -e '.[test]'
pytest                      # unit tests, no network
CRX_LIVE=1 pytest -m live   # live, read-only, no transaction
```
