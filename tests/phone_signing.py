"""Test-only stand-in for G's phone passkey: a throwaway P-256 key that answers a WebAuthn get() like Safari does.

Pure Python (no third-party crypto in CI); deterministic nonces are fine for throwaway test keys and are never used for
anything else. Nothing here is reachable from a CLI: production trust is the root-owned file only.
"""
import base64
import hashlib
import json
import secrets
import struct

from campaign_tool import phone_approval

P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
     0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)
RP_ID = "admin.example.invalid"


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] - 3
        lam = lam * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul(k, point=G):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point = _add(point, point)
        k >>= 1
    return out


def _der_int(n):
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big")
    return b"\x02" + bytes([len(raw)]) + raw


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Phone:
    """One enrolled (or not) passkey for ``rp_id``."""

    def __init__(self, principal="g-phone@example.invalid", rp_id=RP_ID):
        self.principal, self.rp_id = principal, rp_id
        self.secret = secrets.randbelow(N - 1) + 1
        x, y = _mul(self.secret)
        point = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
        self.blob = (phone_approval._s(phone_approval.KEY_TYPE) + phone_approval._s("nistp256") + phone_approval._s(point)
                     + phone_approval._s(rp_id))

    def signers_line(self, namespaces=phone_approval.NAMESPACE):
        return '%s namespaces="%s" %s %s\n' % (self.principal, namespaces, phone_approval.KEY_TYPE,
                                              base64.b64encode(self.blob).decode())

    def _sign(self, data):
        z = int.from_bytes(hashlib.sha256(data).digest(), "big")
        k = int.from_bytes(hashlib.sha256(self.secret.to_bytes(32, "big") + data).digest(), "big") % (N - 1) + 1
        r = _mul(k)[0] % N
        s = pow(k, -1, N) * (z + r * self.secret) % N
        body = _der_int(r) + _der_int(s)
        return b"\x30" + bytes([len(body)]) + body

    def tap(self, envelope, flags=0x1D, origin=None):
        """navigator.credentials.get() output for this envelope, as the console page would post it (UP|UV|BE|BS set)."""
        client = json.dumps({"type": "webauthn.get", "challenge": _b64url(phone_approval.challenge(envelope)),
                             "origin": origin or "https://" + self.rp_id, "crossOrigin": False},
                            separators=(",", ":")).encode()
        auth = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", 0)
        return {"envelope": envelope, "authenticator_data": _b64url(auth), "client_data": _b64url(client),
                "signature": _b64url(self._sign(auth + hashlib.sha256(client).digest()))}
