"""Vulture whitelist — suppress false-positive 'unused' warnings.

Vulture can't see that:
  - Protocol method parameters are part of the contract (not local vars)
  - Context-manager `__exit__` params (exc_type, tb) are required by the protocol
  - The `yield` after `return` in an empty async generator is a Python idiom
    needed to make the function a generator, not a coroutine
"""

from __future__ import annotations

# Protocol method args used at call sites but vulture sees only the def.
spec = ...  # UaClientLike.set_security_string
monitored_item_handles = ...  # SubscriptionBackend.unsubscribe

# Standard context-manager protocol args.
exc_type = ...
tb = ...
