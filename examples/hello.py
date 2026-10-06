"""Balance, one quote, one trade. Needs CRX_WALLET_PK."""
import crx

c = crx.Client(network="testnet")

print(c.balance().free)  # 20000.000000
q = c.quote("USD/BRL", "buy", 25_000)
print(q.pair, q.rate)  # USD/BRL 5.435
t = c.trade(q)
print(t.status, t.tx)  # open 0x9a4e…8a0c
