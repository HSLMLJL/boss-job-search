# Job Radar Core

`radar_core` contains deterministic, platform-neutral listing identity
primitives. It has no browser, account, URL, city-code, network, or source
adapter dependency.

Its public functions are:

- `normalize_text`: conservative comparison normalization.
- `fallback_identity_key`: stable fallback identity when a source has no ID.
- `family_fingerprint`: broad candidate key for possible repost detection.
- `content_fingerprint`: meaningful-content fingerprint that ignores label order.

The local BOSS adapter calls this package, but the package does not know that
adapter exists. A future standalone release must retain this boundary, provide
mock or authorized input adapters, and exclude real job records, account data,
browser extensions, and all BOSS-specific code.
