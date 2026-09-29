"""The 3D shot library: which Blender scene each beat uses, and a cache of rendered shots.

A shot is identified by its scene name, its data parameters and SCENE_VERSION, so a
shot is rendered once and reused every day it's needed. Rendering needs a Python
with `bpy` (set SOCIAL_BLENDER_PYTHON); without one, missing shots are skipped and
the caller falls back to other footage.
"""
import hashlib
import json
import math
import os
import subprocess
from pathlib import Path

SCENE_VERSION = 3
HERE = Path(__file__).parent


def shot_params(scene, fact=None, map_data=None, number=None):
    if scene == "county_pins":
        return {"rings": map_data["rings_local"], "points": map_data["points_local"], "seconds": 5.0}
    if scene == "network_arcs":
        return {"count": int(number or 100), "seconds": 5.0}
    if scene == "retention_blocks":
        return {"days": int(number or 365), "seconds": 5.0}
    return {"seconds": 4.0}


def shot_key(scene, params):
    blob = json.dumps({"scene": scene, "params": params, "v": SCENE_VERSION}, sort_keys=True)
    return f"{scene}-{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


def ensure_shot(scene, params, library, log=print):
    """Return the path of a rendered shot, rendering it if needed and possible; else None."""
    library = Path(library)
    library.mkdir(parents=True, exist_ok=True)
    key = shot_key(scene, params)
    path = library / f"{key}.mp4"
    if path.exists() and path.stat().st_size > 50_000:
        return path
    py = os.environ.get("SOCIAL_BLENDER_PYTHON")
    if not py:
        log(f"3D shot {key} not in library and no SOCIAL_BLENDER_PYTHON set; skipping")
        return None
    pfile = library / f"{key}.params.json"
    pfile.write_text(json.dumps(params))
    log(f"rendering 3D shot {key} (this can take several minutes)")
    subprocess.run([py, str(HERE / "blender_scenes.py"), "--scene", scene, "--out", str(path),
                    "--params", str(pfile)], check=True, stdout=subprocess.DEVNULL)
    return path


def local_map(map_data, span=24.0):
    """Project lon/lat rings and points into Blender units centred on the county."""
    rings, points = map_data["boundary"], map_data["points"]
    lons = [p[0] for r in rings for p in r]
    lats = [p[1] for r in rings for p in r]
    k = math.cos(math.radians(sum(lats) / len(lats)))
    cx, cy = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
    scale = span / max((max(lons) - min(lons)) * k, max(lats) - min(lats))

    def proj(lon, lat):
        return [round((lon - cx) * k * scale, 3), round((lat - cy) * scale, 3)]
    # Thin rings to keep the mesh light.
    rings_local = [[proj(*p) for p in r[:: max(1, len(r) // 400)]] + [proj(*r[0])] for r in rings]
    return dict(map_data, rings_local=rings_local, points_local=[proj(*p) for p in points])


# Which scene illustrates each kind of beat. Facts may override with "scenes".
FRAME_SCENES = {
    "retention": "retention_blocks",
    "sharing": "network_arcs",
    "map": "county_pins",
}
OPENERS = ["camera_hero", "plate_scan"]
