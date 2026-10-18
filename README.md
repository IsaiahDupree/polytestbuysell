# polytestbuysell

Python and Node.js tools for inspecting Polymarket account configuration and
constructing V2 orders, transfers, and wallet operations. This is an archived
experimental toolkit; protocol and SDK behavior must be checked before use.

Local account diagnostics, wallet identities, balances, proxy assignments, and
credentials are excluded from this public source snapshot. Exchange and token
contract addresses in the source are public protocol constants.

Configuration is loaded from `tools/.env`. Inspect each command's help and code
before use. Account-specific signing keys, funder identities, API credentials,
RPC settings, and optional proxy configuration come from the environment.
Never commit that file or account runtime data. The Node order tool reads
`POLYMARKET_PK`, `POLYMARKET_FUNDER`, and `POLYMARKET_SIG_TYPE` from the environment.

The order commands require an explicit execution option before posting an order.
Creating or deriving API credentials can still call the live provider API while
preparing an order. No live trading or provider validation was performed for this
source archival release.
