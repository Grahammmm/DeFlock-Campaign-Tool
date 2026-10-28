"""Digest layer: redaction, deterministic detectors, optional model narrative.

Everything here treats record text as data, never as instructions. ``redact``
runs before any external model call (docs/CONTRACTS.md "Privacy tiers");
``detectors`` produce locator-bound hits that are not findings; ``model``
turns redacted text plus hits into a narrative only when a provider is
configured; ``schema`` validates the digest JSON every consumer relies on.
"""
from .redact import redact  # noqa: F401
from .detectors import run_detectors  # noqa: F401
from .schema import validate_digest, empty_digest  # noqa: F401
from .build import build_digest, redact_units  # noqa: F401
