# Daily Reel pipeline

`python -m campaign_tool.social` makes one finished vertical video (1080×1920, about 18–25 s) per day. It starts from a campaign's published facts and ends with an MP4, a cover image and a caption ready to post on Instagram.

```sh
python -m campaign_tool.social check --campaign <campaign>/social
python -m campaign_tool.social daily --campaign <campaign>/social --out out/ [--date 2026-10-01] [--stub] [--no-llm]
```

## What it does

1. **Plan.** Picks today's fact and format (stat drop, map reveal or records receipt) and avoids repeating a fact within about two weeks. The choice is fixed for a given date, so reruns match.
2. **Script.** Writes the on-screen beats and the caption.
   - If `ANTHROPIC_API_KEY` is set, Claude writes a fresh variant from the fact and the campaign's `messaging.md`.
   - Otherwise, or whenever the variant fails the checks, the fact's own hooks and beats are used.
   - Checks reject any number not in the fact, the campaign's banned terms, overlong lines, and a missing call to action.
3. **Background.** Makes one 8-second 9:16 clip with Google Veo 3.1 (`GEMINI_API_KEY`; model `veo-3.1-fast-generate-preview` by default, override with `SOCIAL_VEO_MODEL`).
   - Clips are kept in a library keyed by prompt, so a prompt is only paid for once.
   - If generation fails, a previous clip is reused.
   - `--stub` makes a plain placeholder so layouts can be checked offline.
4. **Render.** Composes the beats over the clip with Pillow and ffmpeg:
   - hook, count-up stat cards with source lines, quote cards, a map reveal of the campaign's mapped points, and a call-to-action end card;
   - a progress bar, and a note that the background is AI-generated;
   - all text inside Instagram's safe area.
5. **Output.** Writes to `out/<date>/`:
   - `reel.mp4` and `cover.jpg`;
   - `caption.txt`, which has at most 5 hashtags, Instagram's limit;
   - `script.json`;
   - `POSTING.md`, a checklist that covers the AI label, posting as a Trial Reel first, and the pinned comment.

## Campaign files (`<campaign>/social/`)

- `social.json`: brand colours, call to action, caption parts, map files, Veo style and negative prompt, banned terms.
- `facts.json`: published claims only, each with `source_url`, the figures it may show (`numbers`), hooks, beats, a "why it matters" line and a visual description for the background clip. `check` validates every fact against every format.
- `messaging.md`: the campaign's messaging guide, which the Claude writer reads.

`examples/fictional-campaign/social/` is a complete fictional example used by the tests.

## Rules built in

- Only published, sourced facts. The caption quotes the claim and names its source.
- AI video is used as background b-roll only, never as evidence. The default style forbids readable plates, faces, police and cameras. The checklist tells the poster to turn on Instagram's AI label.
- Nothing is posted automatically. A person watches the video, then posts it.

## Costs

- **Veo 3.1 Fast:** about $0.10–0.15 per second of video, so about $1–1.20 for a new 8-second clip and about $30–40 a month for one new clip a day. Library reuse lowers this.
- **Claude script variant:** a few cents a day.
- **Rendering:** runs on a standard GitHub Actions runner in about two minutes.
