"""Run a 100 USD maker test request on Ethereum mainnet.

Set CRX_WALLET_PK or CRX_WALLET_PK_FILE, and CRX_RPC. The maker account
needs USDC and ETH for a deposit. Under 20 USDC of free collateral, the
script deposits the shortfall, at least the 100 USDC minimum, quotes the
returned reference rate as an example price, reads the trade back until it
is final, and returns successfully only when the trade is open and final.
Quote the returned RFQ promptly: it closes after 120 seconds. The request
can renew an unaccepted RFQ for three hours, with the same settlement date.
"""

from crx.quickstart_maker import steps


if __name__ == "__main__":
    steps()
