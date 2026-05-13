"""SHA-256-based server certificate trust list (D-NN-B).

Operators drop trusted server certificates (DER or PEM) into a directory.
Before opening a SecureChannel, we hash the server certificate published by
the chosen endpoint and look for a matching file. Match found ⇒ pass that
file path into asyncua's `set_security_string`. No match ⇒ refuse to connect.

PEM is stripped to DER before hashing so the comparison is on canonical bytes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path

_PEM_BLOCK = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s*(?P<body>[A-Za-z0-9+/=\s]+)-----END CERTIFICATE-----",
    re.MULTILINE,
)


class TrustListMissError(RuntimeError):
    """Raised when no cert in the trust dir matches the server's cert."""


def _file_to_der(path: Path) -> bytes | None:
    """Return the DER bytes of `path`, or None if it isn't a valid cert file."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw.lstrip().startswith(b"-----BEGIN CERTIFICATE-----"):
        m = _PEM_BLOCK.search(raw)
        if m is None:
            return None
        body = b"".join(m.group("body").split())
        try:
            return base64.b64decode(body, validate=True)
        except (ValueError, binascii.Error):
            return None
    if raw.startswith(b"\x30"):
        return raw
    return None


def resolve_server_cert(*, server_cert_der: bytes, trust_dir: Path) -> Path:
    """Return the trust-dir file whose DER bytes hash to the same SHA-256
    as `server_cert_der`. Raise `TrustListMissError` if no file matches.
    """
    target = hashlib.sha256(server_cert_der).digest()
    for entry in sorted(trust_dir.iterdir()):
        if not entry.is_file():
            continue
        der = _file_to_der(entry)
        if der is None:
            continue
        if hashlib.sha256(der).digest() == target:
            return entry
    raise TrustListMissError(f"server certificate not present in trust list {trust_dir!r}")


__all__ = ["TrustListMissError", "resolve_server_cert"]
