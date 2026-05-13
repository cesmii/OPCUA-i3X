"""Generate a self-signed client cert + key for OPC UA SecureChannel auth.

Produces an X.509 cert with the Key Usage / Extended Key Usage / SAN extensions
required by OPC UA Part 6 §6.2.2 (digital_signature, key_encipherment,
data_encipherment; CLIENT_AUTH + SERVER_AUTH; SAN URI matching application_uri).

Outputs:
    certs/client.der                — DER-encoded cert (drop in server trust list)
    certs/client.pem                — PEM-encoded private key
    Prints SHA1 thumbprint + DER path so the operator can register the cert
    with their OPC UA server's trust store.

Re-run safe: refuses to overwrite an existing pair unless `--force` is given.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_APP_URI = "urn:i3xua"
DEFAULT_COMMON_NAME = "i3xua"
DEFAULT_VALIDITY_DAYS = 365


def generate(
    *,
    cert_dir: Path,
    application_uri: str,
    common_name: str,
    validity_days: int,
    force: bool,
) -> tuple[Path, Path, str]:
    cert_path = cert_dir / "client.der"
    key_path = cert_dir / "client.pem"
    if (cert_path.exists() or key_path.exists()) and not force:
        raise SystemExit(
            f"cert pair already exists in {cert_dir}/. Use --force to overwrite."
        )
    cert_dir.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(application_uri)]
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=True,
                data_encipherment=True,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_path.write_bytes(cert_der)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    thumbprint = hashlib.sha1(cert_der).hexdigest().upper()
    return cert_path, key_path, thumbprint


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cert-dir", type=Path, default=Path("certs"))
    p.add_argument("--application-uri", default=DEFAULT_APP_URI)
    p.add_argument("--common-name", default=DEFAULT_COMMON_NAME)
    p.add_argument("--validity-days", type=int, default=DEFAULT_VALIDITY_DAYS)
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    cert_path, key_path, thumbprint = generate(
        cert_dir=args.cert_dir,
        application_uri=args.application_uri,
        common_name=args.common_name,
        validity_days=args.validity_days,
        force=args.force,
    )
    print(f"cert:        {cert_path.resolve()}")
    print(f"key:         {key_path.resolve()}")
    print(f"thumbprint:  {thumbprint}")
    print(f"app uri:     {args.application_uri}")
    print()
    print("Drop the .der into your OPC UA server's trust list to complete")
    print("the client-cert handshake. Server-specific path examples:")
    print("  Kepware: Project Properties → OPC UA → Trust client certificate")
    print("  Reference Server: $LocalApplicationData/OPC Foundation/pki/trusted/certs/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
