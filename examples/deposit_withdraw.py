"""Move USDC in or out. Needs CRX_WALLET_PK and test AVAX for gas.

    python deposit_withdraw.py deposit 1000
    python deposit_withdraw.py withdraw 1000
"""
import sys

import crx

action, amount = sys.argv[1], sys.argv[2]
c = crx.Client(network="testnet")
if action == "deposit":
    d = c.deposit(amount)
    print(d.status, d.txs)  # credited ['0x3b9f…b7d5', '0x8e2d…2e4d', '0x5c7e…5a7c']
elif action == "withdraw":
    w = c.withdraw(amount)
    print(w.status, w.nonce, w.tx)  # accepted 0 0x2f4a…6e1b
else:
    sys.exit("action is deposit or withdraw")
