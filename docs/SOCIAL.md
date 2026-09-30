# Daily Reel pipeline

`python -m campaign_tool.social` makes one finished vertical video (1080×1920, about 20–30 s) per day. It starts from a campaign's published facts and ends with a narrated MP4, a cover image and a caption ready to post on Instagram.

```sh
python -m campaign_tool.social check --campaign <campaign>/social
python -m campaign_tool.social daily --campaign <campaign>/social --out out/ --shots <shot-library> \
    [--date 2026-10-01] [--no-llm] [--offline-voice] [--veo] [--renderer v1]
```

## What it does

1. **Plan.** Picks today's fact and format (stat drop, map reveal or records receipt) and avoids repeating a fact within about two weeks. The choice is fixed for a given date, so reruns match.
2. **Script.** Writes the beats, what the narrator says, and the caption.
   - If `ANTHROPIC_API_KEY` is set, Claude writes a fresh variant from the fact and the campaign's `messaging.md`. Otherwise, or whenever the variant fails the checks, the fact's own hooks and beats are used.
   - Checks cover on-screen text **and spoken lines** (`say`): any number not in the fact, the campaign's banned terms, overlong lines, and a missing call to action are rejected.
3. **Narration.** One voice clip per beat, and each beat lasts as long as its line, so the edit follows the voice (speech in the first seconds holds viewers longer).
   - ElevenLabs if `ELEVENLABS_API_KEY` and a voice id are set (best, and can be the organizer's own cloned voice); otherwise Gemini TTS with `GEMINI_API_KEY` (voice `voice.gemini_voice`, default Charon).
   - `voice.pronounce` maps written forms to spoken ones (`deflockslo.com` → "deflock S L O dot com"); the screen keeps the written form.
   - `--offline-voice` uses espeak-ng for layout tests only; the checklist says not to post those.
4. **3D shots (Blender).** Stylised, clearly illustrative scenes instead of AI video:
   - `camera_hero`: macro on a generic plate-reader camera, pulling back to the pole at dusk;
   - `plate_scan`: a car passes under the camera's scan cone (the plate is blank);
   - `county_pins`: the campaign's real boundary with every mapped location rising as a pin;
   - `network_arcs`: arcs to N other agencies (sharing facts);
   - `retention_blocks`: one block per day of retention, stacked by year.
   Shots are keyed by scene, data and `SCENE_VERSION` in a shot library and rendered once (Cycles, CPU; every 2nd or 3rd frame rendered and the rest interpolated). Rendering needs a Python 3.11 with `bpy` (`requirements-blender.txt`) set as `SOCIAL_BLENDER_PYTHON`; without it, missing shots fall back to a dark field and the graphics still carry the facts.
5. **Render.** Pillow + NumPy + ffmpeg:
   - the hook and "why" lines as large text whose words light up as they are spoken (these are the captions for sound-off viewers); count-up stat cards with source lines; a map count; quote cards; an end card with the campaign's site;
   - the brand and site (`site` in `social.json`) on every frame, plus "3D illustration · sources at <site>";
   - slow push-in, soft vignette, fine grain, short dissolves, a progress bar;
   - synthesized sound design (whooshes, hits, count ticks, a riser, room tone). No music: add a sound in the Instagram app, which the API can't do.
6. **Output.** `out/<date>/`: `reel.mp4`, `cover.jpg`, `caption.txt` (at most 5 hashtags), `script.json`, and `POSTING.md`, a checklist that covers the AI label (AI voice, and AI b-roll if used), posting as a Trial Reel first, and the pinned comment.

`--veo` adds one Google Veo clip as fallback footage for beats without a 3D shot (labelled on screen as AI). `--renderer v1` keeps the original Veo-background renderer.

## Campaign files (`<campaign>/social/`)

- `social.json`: brand colours, `site`, `voice`, call to action (with its spoken `say`), caption parts, map files (with a spoken `say`), Veo style, banned terms.
- `facts.json`: published claims only, each with `source_url`, the figures it may show (`numbers`), hooks, beats (optionally with `say` and `scene`), a "why it matters" line and a visual description. `check` validates every fact against every format.
- `messaging.md`: the campaign's messaging guide, which the Claude writer reads.
- `shots/` (optional): pre-rendered 3D shots to seed the library, so the daily job rarely renders.

`examples/fictional-campaign/social/` is a complete fictional example used by the tests.

## Rules built in

- Only published, sourced facts. The caption quotes the claim and names its source. Spoken lines are checked like on-screen text.
- The 3D scenes are generic illustrations: no real camera brand, no readable plates, no people, no police. The map scene uses the campaign's own published location data.
- Nothing is posted automatically. A person watches the video, then posts it.

## Costs

- **Narration:** Gemini TTS is a fraction of a cent per Reel; ElevenLabs uses a paid plan's character quota (about 400 characters a day).
- **3D shots:** free (CPU time). A new shot takes about 15–30 minutes on a GitHub runner, once.
- **Veo (optional):** about $0.10–0.15 per second of video.
- **Claude script variant:** a few cents a day.
