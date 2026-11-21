"""Pinned addresses of the guardian calls, one block per mode, in the shape of the portal's ``/api/v1/config``.

``core``: the contract that holds the manual price proposals and takes ``cancelManualPrice``.
``lens``: the contract that serves ``pendingManualPrice``; on the Test venue it is ``core``.
``printLens``: the contract that serves ``latestPriceOf``, the CRX last print of a spot feed.
``roles``: the guardian role contract a cancel goes through, or None while the mode takes no cancel.
``roleKey``: the role, ``bytes32("VETO")``.
``startBlock``: the first block the log reads scan.
A zero address is a placeholder: the mode refuses each call until a release fills it.
"""

from __future__ import annotations

ZERO = "0x" + "00" * 20
VETO = "0x" + b"VETO".hex() + "00" * 28

CONFIG = {
    "test": {
        "chainId": 43113,
        "core": "0x9c3CC6203b57728b605F21532FBCD569920D44dE",
        "lens": "0x9c3CC6203b57728b605F21532FBCD569920D44dE",
        "printLens": "0x5e81b2b442cb441897f930d9ab038f9a540e3d30",
        "roles": "0xd4C2D9Fb37D6B3330633Cc53c429d5aC13DbBE53",
        "roleKey": VETO,
        "startBlock": 61406607,
        "rpc": "https://api.avax-test.network/ext/bc/C/rpc",
    },
    "live": {
        "chainId": 1,
        "core": "0x0d716cA9Fc2BFD8c66D2AF2fB38b56D08809A8cc",
        "lens": "0x6d2c103c0ED73703F4e69Ca87E46F93e141405E7",
        "printLens": "0x6d2c103c0ED73703F4e69Ca87E46F93e141405E7",
        "roles": None,
        "roleKey": VETO,
        "startBlock": 26184353,
        "rpc": None,
    },
}

# feedId: the spot feed a fixing or a steward mark names. pairId: keccak256 of the pair's code, which a final price
# names. pythId: the Pyth price id, or None for a pair that CRX prices.
PAIRS = [
    {"pair": "USD/BRL", "code": "USD/BRL",
     "feedId": "0xd2db4dbf1aea74e0f666b0e8f73b9580d407f5e5cf931940b06dc633d7a95906",
     "pairId": "0x644668ae48a22c7afafcf35ff66ebf9c8af2c396ae03de0795d66bde06cdf851",
     "pythId": "0xd2db4dbf1aea74e0f666b0e8f73b9580d407f5e5cf931940b06dc633d7a95906"},
    {"pair": "USD/MXN", "code": "USD/MXN",
     "feedId": "0xe13b1c1ffb32f34e1be9545583f01ef385fde7f42ee66049d30570dc866b77ca",
     "pairId": "0x69f1670ccd0386df6e79b445190cb08f07e9d85ef8bb6ef820417312ea39752f",
     "pythId": "0xe13b1c1ffb32f34e1be9545583f01ef385fde7f42ee66049d30570dc866b77ca"},
    {"pair": "USDC/USDT", "code": "USD/UST",
     "feedId": "0x9eb7a1f7e77d1b80ad522f2c41e818e050456d0846e354d0975c1639dba85b62",
     "pairId": "0x540a8e9da7b2b71bf8b1acabf17c7c7e0151bbb6eb81cadf3b6234f3f7c47c71",
     "pythId": None},
]
