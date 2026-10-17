"""Run a 100 USD maker test request on Ethereum mainnet.

Set CRX_WALLET_PK or CRX_WALLET_PK_FILE, and CRX_RPC. The maker account
needs USDC and ETH for a deposit. The script adds only the shortfall to
20 USDC, quotes the returned reference rate as an example price, and
returns successfully only when the trade is open.
"""

from crx.quickstart_maker import steps


if __name__ == "__main__":
    steps()
