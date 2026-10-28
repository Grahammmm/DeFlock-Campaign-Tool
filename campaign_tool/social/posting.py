"""POSTING.md: the checklist a person works through before posting a Reel.

Dependency-free so it can be tested without the media stack.
"""
from .timeline import AI_VOICES, TEST_VOICES, render_voices


def posting_notes(script, fact, ai_background, cfg):
    render = script.get("render") or {}
    voices = render_voices(render)
    voice = render.get("voice")
    lines = [f"# Reel for {script['date']}", "",
             f"**Fact:** {fact.claim}", f"**Source:** {fact.source_label}: {fact.source_url}", "",
             "## Before you post", "",
             "- [ ] Watch it once with sound off: every number matches the source above.",
             "- [ ] Paste `caption.txt` as the caption.",
             "- [ ] Post as a **Trial Reel** first (shown to non-followers); share to followers if it performs.",
             "- [ ] Optional: add a sound in Instagram, turned down to about 10-15% so the narration stays clear."]
    # Any beat on a test fallback blocks posting, even if earlier beats used an AI voice.
    fallbacks = [v for v in TEST_VOICES if v in voices]
    if fallbacks:
        beats = [str(i + 1) for i, v in enumerate(voices) if v in TEST_VOICES]
        lines.insert(4, f"- [ ] **Don't post this one:** narration used the {' and '.join(fallbacks)} test fallback "
                        f"(beat {', '.join(beats)} of {len(voices)}). "
                        "Check the GEMINI_API_KEY / ELEVENLABS_API_KEY secrets and re-run.")
    ai_voice = any(v in AI_VOICES for v in voices)
    if ai_background or ai_voice:
        why = " and ".join(x for x in ["the narration is an AI voice" if ai_voice else "",
                                        "some background video is AI-generated" if ai_background else ""] if x)
        lines.append(f"- [ ] Turn on **Add AI label** (Advanced settings): {why}.")
    voices_text = ", ".join(voices) if len(set(voices)) > 1 else (voice or "none")
    lines += [f"- [ ] Pin a comment: \"{cfg['caption']['pinned_comment']}\"", "",
              f"Format: {script['format']} · writer: {script['writer']} · voice: {voices_text} · "
              f"3D scenes: {', '.join(render.get('scenes') or []) or 'none'} · "
              f"AI b-roll: {(script.get('clip') or {}).get('model', 'none')}"]
    return "\n".join(lines) + "\n"
