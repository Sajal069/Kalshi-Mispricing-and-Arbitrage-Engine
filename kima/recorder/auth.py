"""Kalshi request signing.

The documented scheme is an RSA-PSS/SHA-256 signature over the concatenation of
``timestamp_ms + METHOD + path``, where **the path excludes the query string**.
Getting that last detail wrong produces a 401 that looks like a credential
problem and is not, so it is called out here and enforced in one place.

Connection-level auth is required even for public market data over the
WebSocket, so the same signer serves both surfaces.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("cryptography is required for Kalshi request signing") from exc


@dataclass
class KalshiAuth:
    """Signs requests with an RSA private key.

    ``key_id`` is the API key identifier from the Kalshi dashboard; ``private_key``
    is the matching RSA key. Neither is ever written to a tape or a report.
    """

    key_id: str
    private_key: rsa.RSAPrivateKey

    @classmethod
    def from_file(cls, key_id: str, path: str | os.PathLike, password: bytes | None = None) -> "KalshiAuth":
        data = Path(path).read_bytes()
        key = serialization.load_pem_private_key(data, password=password)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("Kalshi request signing requires an RSA private key")
        return cls(key_id=key_id, private_key=key)

    @classmethod
    def from_env(cls) -> "KalshiAuth | None":
        """Load from ``KALSHI_KEY_ID`` and ``KALSHI_PRIVATE_KEY_PATH``.

        Returns ``None`` rather than raising when unset, so that the synthetic
        path stays usable with no credentials configured at all.
        """
        key_id = os.environ.get("KALSHI_KEY_ID")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not key_id or not key_path:
            return None
        pw = os.environ.get("KALSHI_PRIVATE_KEY_PASSWORD")
        return cls.from_file(key_id, key_path, pw.encode() if pw else None)

    @staticmethod
    def path_for_signing(url_or_path: str) -> str:
        """The signing path: no scheme, no host, and **no query string**."""
        parts = urlsplit(url_or_path)
        return parts.path or url_or_path.split("?", 1)[0]

    def sign(self, method: str, url_or_path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        path = self.path_for_signing(url_or_path)
        message = f"{ts}{method.upper()}{path}".encode()
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }

    def __repr__(self) -> str:  # pragma: no cover - keeps keys out of logs
        return f"<KalshiAuth key_id={self.key_id[:6]}...>"
