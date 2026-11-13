"""Cloudflare Access checks for requests that arrive on the public Host.

``claims_ok`` is the TRIPWIRE every public-Host request passes: it does NOT verify the JWT signature, so alone it is never
authentication. cloudflared's Access validation ("Protect with Access") does that at the edge; the tripwire only refuses a
request when that edge is missing or misconfigured.

``identity`` (DFB-11) is the check for a decision POST on the public Host: the same claims AND an RS256 signature verified
here, with the standard library, against the Access signing keys in an owner-only JWKS file (``load_keys``; a copy of
``https://<team>.cloudflareaccess.com/cdn-cgi/access/certs``). Only then is the token's email used as the decision's actor.
Every step fails closed. No token part, claim, key, email or aud is ever printed or logged.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import stat

HEADER = "Cf-Access-Jwt-Assertion"
_AUD = re.compile(r"[0-9a-f]{64}")
_TEAM = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\.cloudflareaccess\.com")
_EMAIL = re.compile(r"[^@\s<>\x00-\x1f\x7f]+@[^@\s<>\x00-\x1f\x7f]+")
_PART = re.compile(r"[A-Za-z0-9_-]+")
_SKEW = 60
MAX_KEYS_FILE, MAX_KEYS = 65536, 16
_MIN_BITS, _MAX_BITS = 2048, 8192
# DER DigestInfo prefix for SHA-256 (RFC 8017 section 9.2, note 1).
_SHA256_INFO = bytes((0x30, 0x31, 0x30, 0x0D, 0x06, 0x09, 0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02, 0x01, 0x05, 0x00,
                      0x04, 0x20))


def _owner_only(path, cap):
    """The bytes of an owner-only (no group/other bits), regular, non-symlink file of at most `cap` bytes."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > cap):
            raise ValueError
        raw = b""
        while len(raw) <= cap:
            chunk = os.read(fd, cap + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    if len(raw) > cap:
        raise ValueError
    return raw


def load_config(path):
    """Read the owner-only JSON config once; raise ValueError('access_config_invalid') on any defect."""
    try:
        raw = _owner_only(path, 4096)
        cfg = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_refuse)
        if (len(raw) > 4096 or not isinstance(cfg, dict) or set(cfg) != {"aud", "email", "team_domain"}
                or not all(isinstance(v, str) for v in cfg.values())
                or not _AUD.fullmatch(cfg["aud"]) or not _TEAM.fullmatch(cfg["team_domain"])
                or len(cfg["email"]) > 254 or not _EMAIL.fullmatch(cfg["email"])):
            raise ValueError
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError("access_config_invalid") from None
    return cfg


def _refuse(_):
    raise ValueError


def _no_dupes(pairs):
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError
    return out


def _decode(part):
    obj = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8"),
                     object_pairs_hook=_no_dupes, parse_constant=_refuse)
    if not isinstance(obj, dict):
        raise ValueError
    return obj


def _int(value):
    return type(value) is int


def claims_ok(headers, cfg, now):
    """True only when the one assertion header carries claims matching cfg at time `now`."""
    return _checked(headers, cfg, now) is not None


def _checked(headers, cfg, now):
    """(token parts, header) when the one assertion header carries claims matching cfg at `now`; else None."""
    try:
        values = headers.get_all(HEADER) or []
        if len(values) != 1 or cfg is None:
            return None
        token = values[0]
        if not token.isascii() or len(token) > 8192:
            return None
        parts = token.split(".")
        if len(parts) != 3 or not all(_PART.fullmatch(p) for p in parts):
            return None
        head, claims = _decode(parts[0]), _decode(parts[1])
        aud = claims.get("aud")
        if head.get("alg") != "RS256":
            return None
        if not (aud == cfg["aud"] or (isinstance(aud, list) and len(aud) <= 16
                                      and all(isinstance(a, str) for a in aud) and cfg["aud"] in aud)):
            return None
        if claims.get("iss") != "https://" + cfg["team_domain"]:
            return None
        exp, iat = claims.get("exp"), claims.get("iat")
        if not _int(exp) or exp <= now or not _int(iat) or iat > now + _SKEW:
            return None
        if "nbf" in claims and (not _int(claims["nbf"]) or claims["nbf"] > now + _SKEW):
            return None
        email = claims.get("email")
        if isinstance(email, str) and email.casefold() == cfg["email"].casefold():
            return parts, head
        return None
    except Exception:  # noqa: BLE001 - every failure is a refusal
        return None


def _b64(part):
    if not isinstance(part, str) or not _PART.fullmatch(part):
        raise ValueError
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def load_keys(path):
    """{kid: (n, e)} from an owner-only JWKS file (at most 64 KiB, at most 16 keys). Only RSA signing keys of 2048 to 8192
    bits are kept; other members of the file (e.g. Cloudflare's ``public_cert``) are ignored. ValueError
    ('access_keys_invalid') on any defect or when no usable key remains."""
    try:
        raw = _owner_only(path, MAX_KEYS_FILE)
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_refuse)
        listed = doc.get("keys") if isinstance(doc, dict) else None
        if not isinstance(listed, list) or not 0 < len(listed) <= MAX_KEYS:
            raise ValueError
        keys = {}
        for jwk in listed:
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or jwk.get("alg", "RS256") != "RS256" \
                    or jwk.get("use", "sig") != "sig":
                continue
            kid = jwk.get("kid")
            if not (isinstance(kid, str) and 0 < len(kid) <= 128 and kid.isascii() and kid.isprintable()) or kid in keys:
                raise ValueError
            n, e = int.from_bytes(_b64(jwk.get("n")), "big"), int.from_bytes(_b64(jwk.get("e")), "big")
            if not (_MIN_BITS <= n.bit_length() <= _MAX_BITS and n % 2 and 3 <= e < n and e % 2):
                raise ValueError
            keys[kid] = (n, e)
        if not keys:
            raise ValueError
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("access_keys_invalid") from None
    return keys


def rs256_valid(signing_input, signature, key):
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 8.2.2): the whole encoded block is rebuilt and compared, never parsed."""
    n, e = key
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    digest = _SHA256_INFO + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (size - 3 - len(digest)) + b"\x00" + digest
    return hmac.compare_digest(pow(s, e, n).to_bytes(size, "big"), expected)


def identity(headers, cfg, keys_path, now):
    """The configured email when the one assertion header passes ``claims_ok`` AND its RS256 signature verifies with the
    key its ``kid`` names in the keys file (re-read on every call, so a refreshed file needs no restart); else None.
    ValueError('access_keys_invalid') when the keys file itself is unusable."""
    keys = load_keys(keys_path)
    checked = _checked(headers, cfg, now)
    if checked is None:
        return None
    parts, head = checked
    try:
        key = keys.get(head.get("kid")) if isinstance(head.get("kid"), str) else None
        if key is None or not rs256_valid((parts[0] + "." + parts[1]).encode("ascii"), _b64(parts[2]), key):
            return None
    except (ValueError, TypeError):
        return None
    return cfg["email"]
