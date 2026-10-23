"""Read the balance, then request and accept one quote on testnet with the key in CRX_WALLET_PK."""
import time

import crx

c = crx.Client(network="testnet")

print(c.balance().free)  # 20000.000000
q = c.quote("USD/BRL", "buy", 25_000, settles=int(time.time()) + 7 * 86_400)  # Unix seconds, one week out
print(q.pair, q.rate)  # USD/BRL 5.435
t = c.accept(q)
print(t.status, t.tx)  # open 0x9a4e…8a0c
