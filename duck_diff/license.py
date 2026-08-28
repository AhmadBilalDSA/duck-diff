"""License management for duck-diff (activation, validation, status).

License tiers
-------------
* **Community** — free forever; no license key required.
* **Pro Annual** — $29/year; requires a valid annual license key.
* **Founder Lifetime** — $49 one-time; requires a valid lifetime key.

License keys are Ed25519-signed payloads containing the tier, HWID
fingerprint, and expiry timestamp.  Activation stores the signed record
in a platform-appropriate config directory; validation re-verifies the
signature on every call.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .security import (
    Ed25519PublicKey,
    fingerprint_hwid,
    generate_keypair,
    sign,
    verify,
)

__all__ = [
    "LicenseInfo",
    "LicenseTier",
    "activate_license",
    "deactivate_license",
    "get_license_info",
    "is_license_valid",
]

# ---------------------------------------------------------------------------
# Embedded keypair (generated once at build time; public key is distributed)
# ---------------------------------------------------------------------------
# For the open-source Community tier the key pair is re-generated on first
# activation so each installation has its own authority.  The private seed
# is stored **only** in memory during activation — never written to disk.

_DEFAULT_SEED = b"\x00" * 32  # placeholder; overwritten at activation time
_DEFAULT_PUB = Ed25519PublicKey(b"\x00" * 32)


def _license_dir() -> Path:
    """Return the platform-appropriate config directory for duck-diff."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif os.name == "posix" and platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "duck-diff"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _license_path() -> Path:
    return _license_dir() / "license.json"


# ---------------------------------------------------------------------------
# Tier enumeration
# ---------------------------------------------------------------------------


class LicenseTier:
    """License tier constants."""

    COMMUNITY = "community"
    PRO_ANNUAL = "pro_annual"
    FOUNDER_LIFETIME = "founder_lifetime"

    ALL = (COMMUNITY, PRO_ANNUAL, FOUNDER_LIFETIME)

    # Tier features / restrictions
    WEB_STUDIO = {
        COMMUNITY: False,
        PRO_ANNUAL: True,
        FOUNDER_LIFETIME: True,
    }
    REPORT_EXPORT = {
        COMMUNITY: False,
        PRO_ANNUAL: True,
        FOUNDER_LIFETIME: True,
    }
    PRIORITY_UPDATES = {
        COMMUNITY: False,
        PRO_ANNUAL: False,
        FOUNDER_LIFETIME: True,
    }


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LicenseInfo:
    """Structured representation of a persisted license."""

    tier: str = LicenseTier.COMMUNITY
    hwid: str = ""
    activated_at: float = 0.0
    expires_at: float = 0.0
    public_key_hex: str = ""
    signature_hex: str = ""
    is_valid: bool = False

    @property
    def is_expired(self) -> bool:
        if self.expires_at <= 0:
            return False  # lifetime or community
        return time.time() > self.expires_at

    @property
    def days_remaining(self) -> Optional[int]:
        if self.expires_at <= 0:
            return None  # unlimited
        remaining = self.expires_at - time.time()
        return max(0, int(remaining // 86400))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "hwid": self.hwid,
            "activated_at": self.activated_at,
            "expires_at": self.expires_at,
            "public_key_hex": self.public_key_hex,
            "signature_hex": self.signature_hex,
            "is_valid": self.is_valid,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LicenseInfo":
        return cls(
            tier=data.get("tier", LicenseTier.COMMUNITY),
            hwid=data.get("hwid", ""),
            activated_at=float(data.get("activated_at", 0)),
            expires_at=float(data.get("expires_at", 0)),
            public_key_hex=data.get("public_key_hex", ""),
            signature_hex=data.get("signature_hex", ""),
            is_valid=bool(data.get("is_valid", False)),
        )


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

_ANNUAL_TTL = 365 * 86400  # 1 year in seconds


def _build_payload(
    tier: str, hwid: str, activated_at: float, expires_at: float
) -> bytes:
    """Canonical bytes that are signed for the license."""
    record = {
        "tier": tier,
        "hwid": hwid,
        "activated_at": activated_at,
        "expires_at": expires_at,
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def activate_license(
    license_key: str,
    *,
    tier: str = LicenseTier.PRO_ANNUAL,
) -> LicenseInfo:
    """Activate a license key and persist it.

    The *license_key* is a base64-encoded JSON object containing at minimum
    a ``signature`` and ``public_key`` field signed over the tier / HWID /
    timestamps.

    For the Community tier activation is automatic (no key required).

    Returns:
        A :class:`LicenseInfo` describing the result.
    """
    hwid = fingerprint_hwid()
    now = time.time()

    if tier == LicenseTier.COMMUNITY:
        # Community tier: generate an ephemeral key, sign locally.
        seed, pubkey = generate_keypair()
        expires_at = 0.0  # unlimited
        payload = _build_payload(tier, hwid, now, expires_at)
        sig = sign(seed, payload)
        info = LicenseInfo(
            tier=tier,
            hwid=hwid,
            activated_at=now,
            expires_at=expires_at,
            public_key_hex=pubkey.to_bytes().hex(),
            signature_hex=sig.hex(),
            is_valid=True,
        )
        _persist(info)
        return info

    # Pro / Founder tiers: decode the license key
    try:
        import base64

        raw = base64.urlsafe_b64decode(license_key + "==")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return LicenseInfo(tier=tier, hwid=hwid, is_valid=False)

    sig_hex = data.get("signature", "")
    pub_hex = data.get("public_key", "")
    lic_hwid = data.get("hwid", "")
    lic_tier = data.get("tier", tier)
    lic_activated = float(data.get("activated_at", now))
    lic_expires = float(data.get("expires_at", 0))

    # Validate tier
    if lic_tier not in LicenseTier.ALL:
        return LicenseInfo(tier=tier, hwid=hwid, is_valid=False)

    # Validate HWID binding
    if lic_hwid and lic_hwid != hwid:
        return LicenseInfo(tier=lic_tier, hwid=hwid, is_valid=False)

    # For annual: enforce TTL
    if lic_tier == LicenseTier.PRO_ANNUAL and lic_expires <= 0:
        lic_expires = lic_activated + _ANNUAL_TTL

    # Verify signature
    pubkey = Ed25519PublicKey.from_bytes(bytes.fromhex(pub_hex))
    payload = _build_payload(lic_tier, lic_hwid or hwid, lic_activated, lic_expires)
    sig_bytes = bytes.fromhex(sig_hex)
    valid = verify(pubkey, payload, sig_bytes)

    info = LicenseInfo(
        tier=lic_tier,
        hwid=hwid,
        activated_at=lic_activated,
        expires_at=lic_expires,
        public_key_hex=pub_hex,
        signature_hex=sig_hex,
        is_valid=valid and not (lic_expires > 0 and time.time() > lic_expires),
    )
    _persist(info)
    return info


def get_license_info() -> LicenseInfo:
    """Read the persisted license; returns Community tier if none exists."""
    path = _license_path()
    if not path.exists():
        return LicenseInfo(tier=LicenseTier.COMMUNITY, is_valid=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        info = LicenseInfo.from_dict(data)
        # Re-validate
        if info.signature_hex and info.public_key_hex:
            hwid = fingerprint_hwid()
            if info.hwid and info.hwid != hwid:
                info.is_valid = False
            else:
                pubkey = Ed25519PublicKey.from_bytes(bytes.fromhex(info.public_key_hex))
                payload = _build_payload(info.tier, info.hwid, info.activated_at, info.expires_at)
                sig_bytes = bytes.fromhex(info.signature_hex)
                info.is_valid = verify(pubkey, payload, sig_bytes) and not info.is_expired
        return info
    except Exception:  # noqa: BLE001
        return LicenseInfo(tier=LicenseTier.COMMUNITY, is_valid=True)


def is_license_valid() -> bool:
    """Quick check: is the current license valid and not expired?"""
    return get_license_info().is_valid


def deactivate_license() -> None:
    """Remove the persisted license file (reverts to Community tier)."""
    path = _license_path()
    if path.exists():
        path.unlink()


def _persist(info: LicenseInfo) -> None:
    """Write license info to disk."""
    path = _license_path()
    path.write_text(json.dumps(info.to_dict(), indent=2), encoding="utf-8")


def generate_license_key(
    tier: str,
    hwid: str,
    *,
    duration_seconds: int = _ANNUAL_TTL,
) -> str:
    """Generate a signed license key (used by the licensing server / tests).

    Returns a base64url-encoded JSON string suitable for :func:`activate_license`.
    """
    import base64

    seed, pubkey = generate_keypair()
    now = time.time()
    expires = now + duration_seconds if tier != LicenseTier.FOUNDER_LIFETIME else 0.0
    payload = _build_payload(tier, hwid, now, expires)
    sig = sign(seed, payload)
    record = {
        "tier": tier,
        "hwid": hwid,
        "activated_at": now,
        "expires_at": expires,
        "public_key": pubkey.to_bytes().hex(),
        "signature": sig.hex(),
    }
    return base64.urlsafe_b64encode(json.dumps(record).encode()).decode().rstrip("=")
