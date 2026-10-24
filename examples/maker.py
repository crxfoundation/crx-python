"""Run a 100 USD maker test request on Ethereum mainnet.

Set CRX_WALLET_PK or CRX_WALLET_PK_FILE. The maker account needs USDC and ETH for a deposit.
"""

from crx.quickstart_maker import steps


if __name__ == "__main__":
    steps()
