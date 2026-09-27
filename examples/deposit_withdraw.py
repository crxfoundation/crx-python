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
    print("deposit txs", d.txs, "- collateral after the next fold")
elif action == "withdraw":
    w = c.withdraw(amount)
    print("withdraw armed, nonce", w.nonce, "tx", w.tx, "- paid after the next fold and crank")
else:
    sys.exit("action is deposit or withdraw")
