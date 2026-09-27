"""Balance, one quote, one trade. Needs CRX_WALLET_PK."""
import crx

c = crx.Client(network="testnet")

try:
    print("free", c.balance().free)
    q = c.quote("USD/MXN", "buy", 25_000)
    print("quote", q.rate)
    t = c.trade(q)
    print(t.status, t.tx)
except crx.MarketClosed as e:
    print("closed, opens at", e.details["opens_at"])
except crx.CrxError as e:
    print(e.code, e)
