"""Make today's Reel.

    python -m campaign_tool.social daily --campaign <dir> --out <dir> [--date YYYY-MM-DD] [--stub]
    python -m campaign_tool.social check --campaign <dir>

The campaign directory holds social.json (brand, captions, map files, Veo style),
facts.json (published facts with sources) and messaging.md (the messaging guide).
"""
import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path

from .facts import load_facts
from .render import Brand, Renderer, load_map
from .script import check_script, llm_script, plan, template_script
from .veo import get_clip, library_fallback


def load_campaign(root):
    root = Path(root)
    cfg = json.loads((root / "social.json").read_text())
    facts = load_facts(root / "facts.json")
    guide = (root / "messaging.md").read_text() if (root / "messaging.md").exists() else ""
    return cfg, facts, guide


def daily(args):
    root = Path(args.campaign)
    cfg, facts, guide = load_campaign(root)
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    history_path = Path(args.history) if args.history else Path(args.out) / "history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    fact, fmt = plan(facts, day, history, cfg.get("formats"))
    if args.format:
        fmt = args.format
    print(f"{day}: fact {fact.id}, format {fmt}")

    script = None if args.no_llm else llm_script(fact, fmt, cfg, day, guide)
    script = script or template_script(fact, fmt, cfg, day)
    problems = check_script(script, fact, cfg)
    if problems:
        raise SystemExit("Script failed checks:\n  " + "\n  ".join(problems))

    library = Path(args.library or Path(args.out) / "clip-library")
    try:
        clip, clip_info = get_clip(script["veo_prompt"], library, cfg["veo"].get("negative", ""), stub=args.stub)
    except Exception as exc:
        print(f"Veo generation failed ({exc}); reusing a library clip")
        clip, clip_info = library_fallback(library)
        if not clip:
            clip, clip_info = get_clip(script["veo_prompt"], library, stub=True)
    ai_background = bool(clip_info.get("ai_generated"))

    out = Path(args.out) / day.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    m = cfg.get("map", {})
    map_data = None
    if m.get("boundary") and m.get("points"):
        map_data = load_map(root / m["boundary"], root / m["points"], root / m["roads"] if m.get("roads") else None)
    renderer = Renderer(Brand(cfg["brand"]), map_data)
    stats = renderer.render(script, clip, out / "reel.mp4", ai_background)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", "1.4", "-i", str(out / "reel.mp4"),
                    "-frames:v", "1", "-q:v", "3", str(out / "cover.jpg")], check=True)

    script.update(clip=clip_info, render=stats)
    (out / "script.json").write_text(json.dumps(script, indent=2))
    (out / "caption.txt").write_text(script["caption"] + "\n")
    (out / "POSTING.md").write_text(posting_notes(script, fact, ai_background, cfg))
    history = [h for h in history if h.get("date") != day.isoformat()]
    history.append({"date": day.isoformat(), "fact_id": fact.id, "format": fmt, "writer": script["writer"],
                    "clip": clip_info.get("key"), "ai_background": ai_background})
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(history[-120:], indent=2))
    print(f"Wrote {out / 'reel.mp4'} ({stats['seconds']} s)")
    return out


def posting_notes(script, fact, ai_background, cfg):
    lines = [f"# Reel for {script['date']}", "",
             f"**Fact:** {fact.claim}", f"**Source:** {fact.source_label}: {fact.source_url}", "",
             "## Before you post", "",
             "- [ ] Watch it once with sound off: every number matches the source above.",
             "- [ ] Paste `caption.txt` as the caption.",
             "- [ ] Post as a **Trial Reel** first (shown to non-followers); share to followers if it performs.",
             "- [ ] Optional: add a trending sound at low volume in Instagram."]
    if ai_background:
        lines.append("- [ ] Turn on **Add AI label** (Advanced settings): the background video is AI-generated.")
    lines += [f"- [ ] Pin a comment: \"{cfg['caption']['pinned_comment']}\"", "",
              f"Format: {script['format']} · writer: {script['writer']} · background: "
              f"{script['clip'].get('model', 'placeholder')}"]
    return "\n".join(lines) + "\n"


def check(args):
    cfg, facts, _ = load_campaign(args.campaign)
    day = dt.date.today()
    for fact in facts:
        from .script import FORMATS
        for fmt in FORMATS:
            problems = check_script(template_script(fact, fmt, cfg, day), fact, cfg)
            if problems:
                raise SystemExit(f"{fact.id}/{fmt}: " + "; ".join(problems))
    print(f"{len(facts)} facts OK")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daily")
    d.add_argument("--campaign", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--date")
    d.add_argument("--format")
    d.add_argument("--history")
    d.add_argument("--library")
    d.add_argument("--stub", action="store_true", help="placeholder background instead of Veo")
    d.add_argument("--no-llm", action="store_true", help="template script only")
    c = sub.add_parser("check")
    c.add_argument("--campaign", required=True)
    args = p.parse_args()
    daily(args) if args.cmd == "daily" else check(args)


if __name__ == "__main__":
    main()
