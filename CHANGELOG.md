# Changelog

## 0.3.16

- `quote()` and `ask()` need `settles=` (or the older `expiry=`); there is no default. A call with neither raises `BadRequest` that names the accepted forms.
- `settles=` takes an exact UTC time only: a datetime with a timezone, an ISO 8601 time with `Z` or an offset (`"2026-10-24T15:00Z"`), or a Unix time in seconds as an integer (`int` or any `numbers.Integral`, such as a numpy integer) or a float (`1792854000`). The time is sent as given.
- An exact time must be at least the shortest tenor plus 3 minutes from now (the time to open the request), outside the next long close and within the longest tenor. Else `BadRequest` before any request, with `details['next_time']`. A request sent again after a collateral refusal is checked again with the same rule.
- `settles=` no longer takes a tenor (`"1D"`, `"1W"`, `"1M"`), an hour tenor (`"1H"` to `"23H"`) or a date (`"2026-12-18"`): each raises `BadRequest` that names the accepted forms. A bool, or a number of 100,000,000,000 or more (milliseconds or finer), raises `BadRequest`. `expiry=` is unchanged.
- The `tzdata` dependency on Windows is removed: no code reads a time zone database.
- The Solana network is removed: `network="solana"`, `Client(keypair=, check_rpc_url=)`, `CRX_CHECK_RPC`, `bind()`, `bind_state()`, `deposit(keypair=, unsigned=, authority=)`, the errors `SeatStopped`, `SeatBoundOtherPayout`, `BindInProgress` and `BindFailed`, and the `crx-python[solana]` extra. `network="solana"` raises `ConfigError` (unknown network); a removed argument raises `TypeError`. Testnet and mainnet are unchanged.
- On testnet, a `/health` chain id other than 43113 raises `ConfigError` ("not a testnet"). Nothing is signed, as before.
