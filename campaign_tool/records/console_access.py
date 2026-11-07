"""Access-claims TRIPWIRE for requests that arrive on the public Host.

This does NOT verify the JWT signature, so it is never authentication. cloudflared's Access validation
("Protect with Access") does that; this only refuses a request when that edge is missing or misconfigured.
Every step fails closed. No token part, claim, email or aud is ever returned, printed or logged.
"""
import base64
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


def load_config(path):
    """Read the owner-only JSON config once; raise ValueError('access_config_invalid') on any defect."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > 4096):
                raise ValueError
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
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
    try:
        values = headers.get_all(HEADER) or []
        if len(values) != 1 or cfg is None:
            return False
        token = values[0]
        if not token.isascii() or len(token) > 8192:
            return False
        parts = token.split(".")
        if len(parts) != 3 or not all(_PART.fullmatch(p) for p in parts):
            return False
        head, claims = _decode(parts[0]), _decode(parts[1])
        aud = claims.get("aud")
        if head.get("alg") != "RS256":
            return False
        if not (aud == cfg["aud"] or (isinstance(aud, list) and len(aud) <= 16
                                      and all(isinstance(a, str) for a in aud) and cfg["aud"] in aud)):
            return False
        if claims.get("iss") != "https://" + cfg["team_domain"]:
            return False
        exp, iat = claims.get("exp"), claims.get("iat")
        if not _int(exp) or exp <= now or not _int(iat) or iat > now + _SKEW:
            return False
        if "nbf" in claims and (not _int(claims["nbf"]) or claims["nbf"] > now + _SKEW):
            return False
        email = claims.get("email")
        return isinstance(email, str) and email.casefold() == cfg["email"].casefold()
    except Exception:  # noqa: BLE001 - every failure is a refusal
        return False
