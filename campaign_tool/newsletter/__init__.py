"""Newsletter drafts from published findings and upcoming meetings.

``build_draft(manifest)`` returns ``{"subject", "html", "text", "counts"}``
from a manifest the workspace assembles (published findings since the last
send, upcoming meetings, campaign name and base URL). It contains no personal
data by construction: the manifest never carries subscriber identities, and
unsubscribe is handled by the provider through the ``{{ unsubscribe }}``
placeholder Brevo substitutes per recipient. Sending is a separate
``send_newsletter`` approval card; nothing here sends.
"""
from .draft import UNSUBSCRIBE_PLACEHOLDER, build_draft, load_manifest, write_draft

__all__ = ["UNSUBSCRIBE_PLACEHOLDER", "build_draft", "load_manifest", "write_draft"]
