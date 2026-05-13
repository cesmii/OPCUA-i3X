"""Subclass of `asyncua.Client` that overrides `CreateSessionRequest.ServerUri`.

asyncua synthesizes `params.ServerUri` from the endpoint URL; strict servers
reject any mismatch with `BadServerUriInvalid`. We override that field at the
last moment before the CreateSessionRequest hits the wire.

Endpoint matching here is policy/mode-driven, decoupled from auth (see the
ChannelSecurity / UserIdentity split in settings.py).
"""

from __future__ import annotations

from typing import Any

from asyncua import Client, ua

_POLICY_URI_PREFIX = "http://opcfoundation.org/UA/SecurityPolicy#"


class _UriAwareClient(Client):  # type: ignore[misc]
    """asyncua.Client with a settable `CreateSessionRequest.ServerUri`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._override_server_uri: str | None = None
        self._install_uri_override()

    def _install_uri_override(self) -> None:
        original = self.uaclient.create_session

        async def patched(params: ua.CreateSessionParameters) -> Any:
            override = self._override_server_uri
            if override is not None:
                params.ServerUri = override
            return await original(params)

        self.uaclient.create_session = patched


def pick_matching_endpoint(
    endpoints: list[Any],
    *,
    policy: str,
    mode: ua.MessageSecurityMode,
) -> Any | None:
    """Return the first endpoint whose policy tail and mode match. None if no match.

    Underscore tolerance: OPC UA spec URI tails use mixed conventions —
    ``Basic256Sha256`` (no underscores, legacy) but ``Aes256_Sha256_RsaPss``
    (with underscores). asyncua's ``set_security_string`` and our config
    literals use the underscoreless form for both. We normalize underscores
    out of the URI tail before comparing so AES endpoints match.
    """
    target = policy.replace("_", "")
    for ep in endpoints:
        if ep.SecurityMode != mode:
            continue
        uri = ep.SecurityPolicyUri
        if not uri.startswith(_POLICY_URI_PREFIX):
            continue
        tail = uri.removeprefix(_POLICY_URI_PREFIX).replace("_", "")
        if tail == target:
            return ep
    return None


__all__ = ["_UriAwareClient", "pick_matching_endpoint"]
