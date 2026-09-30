"""Daily short-video pipeline: campaign facts -> script -> AI background clips -> finished Reel.

The pipeline only uses facts the campaign has already published, each with a
public source link. Every number that appears on screen or in the caption must
come from the fact being used; the validator rejects anything else.
"""
