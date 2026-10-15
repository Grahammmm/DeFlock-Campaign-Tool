# Authenticated reviewer boundary audit

Issue #5 source audit on main c6a1b34 found that records/review_bundle.py already
authenticates opaque sessions against an installed Authority, signs immutable
receipts, binds exact evidence/public bytes and requires independent roles.
The older campaign_tool/review.py explicitly provides content checks only;
it is not a replacement authentication path.

This bounded addition exercises other signing keys, changed campaign profiles
even with the same key, authentic receipts transplanted between bundles, and
exact replay after reconstructing the same trusted authority. Existing tests
cover self-review, role impersonation, tampering, content changes and independence.
No authentication implementation gap was reproduced in these cases. These tests
close a cross-authority regression-coverage gap, not issue #5 or live enrollment.

Synthetic Authority defaults remain test_only: completed synthetic reviews and
owner receipts never establish production review, handoff or publication.
Owner-installed principal authentication, independent factual/legal/privacy review,
reviewed legal packages and publication approval remain separate live holds.
