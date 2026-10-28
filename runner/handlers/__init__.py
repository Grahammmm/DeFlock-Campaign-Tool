"""Job handlers. Each exposes ``run(ctx) -> runner.loop.Result``."""
import hashlib
import json


def idempotency_key(kind, *parts):
    """sha256 of kind + inputs, matching the Worker's `sha256Hex(kind + material)` habit."""
    material = kind + "".join(json.dumps(p, sort_keys=True) if not isinstance(p, str) else p for p in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()
