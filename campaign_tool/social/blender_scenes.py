"""Stylized 3D shots rendered headless with Blender (bpy), for the daily Reel.

Run with a Python that has `bpy` installed (it pins its own numpy, so keep it in a
separate virtualenv):

    python -m campaign_tool.social.blender_scenes --scene camera_hero --out shot.mp4 [--params '{...}']

Every scene uses one look: a clean, stylized night palette (deep teal, sodium amber,
coral scan light), simple geometry and slow camera moves. No photoreal people, faces,
plates or logos. Data scenes (county pins, network arcs, retention blocks) are driven
by real campaign numbers passed in `--params`.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector

W, H = int(os.environ.get("SOCIAL_3D_W", 720)), int(os.environ.get("SOCIAL_3D_H", 1280))
INK = (0.012, 0.055, 0.058)
TEAL = (0.02, 0.16, 0.17)
MINT = (0.36, 0.78, 0.70)
CORAL = (0.72, 0.13, 0.07)
AMBER = (1.0, 0.55, 0.18)
CREAM = (1.0, 0.96, 0.9)


# ---------------------------------------------------------------- setup helpers
def reset(fps=24, seconds=4.0, samples=int(os.environ.get("SOCIAL_3D_SAMPLES", 8))):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    s = bpy.context.scene
    s.render.engine = "CYCLES"
    s.cycles.device = "CPU"
    s.cycles.samples = samples
    s.cycles.use_denoising = True
    s.cycles.max_bounces = 3
    s.cycles.use_adaptive_sampling = True
    s.render.use_persistent_data = True
    s.render.resolution_x, s.render.resolution_y = W, H
    s.render.fps = fps
    s.frame_start, s.frame_end = 1, int(fps * seconds)
    s.render.film_transparent = False
    s.view_settings.view_transform = "AgX"
    s.view_settings.look = "AgX - Medium High Contrast"
    world = bpy.data.worlds.new("world")
    s.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (*INK, 1)
    bg.inputs[1].default_value = 1.0
    glare()
    return s


def glare():
    """Soft bloom on emissive lights (lens, pins, arcs): the one 'cinematic' post effect."""
    s = bpy.context.scene
    try:
        s.use_nodes = True
        tree = s.node_tree
    except AttributeError:
        tree = bpy.data.node_groups.new("comp", "CompositorNodeTree")
        s.compositing_node_group = tree
    nodes = tree.nodes
    rl = nodes.get("Render Layers") or nodes.new("CompositorNodeRLayers")
    out = nodes.get("Composite") or nodes.new("CompositorNodeComposite") if "CompositorNodeComposite" in dir(bpy.types) else None
    g = nodes.new("CompositorNodeGlare")
    settings = {"Type": "Bloom", "Quality": "Medium", "Threshold": 1.2, "Strength": 0.35, "Size": 0.6}
    for name, val in settings.items():
        if name in g.inputs:  # Blender 4.4+/5: settings are input sockets
            try:
                g.inputs[name].default_value = val
            except TypeError:
                if name == "Type":
                    g.inputs[name].default_value = "Fog Glow"
    for attr, val in (("glare_type", "FOG_GLOW"), ("quality", "MEDIUM"), ("size", 7), ("threshold", 1.2), ("mix", -0.6)):
        try:
            setattr(g, attr, val)  # older Blender
        except (AttributeError, TypeError):
            pass
    try:
        tree.links.new(rl.outputs["Image"], g.inputs["Image"])
        if out is None:
            out = nodes.new("NodeGroupOutput")
            tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        tree.links.new(g.outputs["Image"], out.inputs[0])
    except Exception as exc:  # compositor API differs between Blender versions; bloom is optional
        print("glare skipped:", exc)


def backdrop():
    """Curved sky card with a teal-to-ink gradient and a faint hill line, far behind the set."""
    m = bpy.data.materials.new("sky")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    tex = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (*INK, 1)
    ramp.color_ramp.elements[1].color = (0.05, 0.22, 0.24, 1)
    ramp.color_ramp.elements[1].position = 0.9
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.inputs[1].default_value = 1.0
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(tex.outputs["Generated"], sep.inputs[0])
    nt.links.new(sep.outputs["Z"], ramp.inputs[0])
    nt.links.new(ramp.outputs["Color"], emit.inputs[0])
    nt.links.new(emit.outputs[0], out.inputs[0])
    card = add(bpy.ops.mesh.primitive_plane_add, "sky", m, size=1, location=(0, 60, 20), scale=(140, 40, 1))
    card.rotation_euler = (math.radians(90), 0, 0)


def streetlight(at, color=AMBER, energy=180):
    light = bpy.data.lights.new("sodium", "POINT")
    light.color = color
    light.energy = energy
    light.shadow_soft_size = 0.4
    o = bpy.data.objects.new("sodium", light)
    bpy.context.collection.objects.link(o)
    o.location = at
    return o


def mat(name, color, rough=0.5, metal=0.0, emit=None, strength=0.0, alpha=1.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    p = m.node_tree.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = (*color, 1)
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = metal
    if emit:
        p.inputs["Emission Color"].default_value = (*emit, 1)
        p.inputs["Emission Strength"].default_value = strength
    if alpha < 1:
        p.inputs["Alpha"].default_value = alpha
        m.blend_method = "BLEND" if hasattr(m, "blend_method") else None
    return m


def add(obj_op, name, material=None, **kw):
    obj_op(**kw)
    o = bpy.context.active_object
    o.name = name
    if material:
        o.data.materials.append(material)
    return o


def camera(loc, target, lens=35):
    cam_data = bpy.data.cameras.new("cam")
    cam_data.lens = lens
    cam_data.dof.use_dof = True
    cam_data.dof.aperture_fstop = 2.8
    cam = bpy.data.objects.new("cam", cam_data)
    bpy.context.collection.objects.link(cam)
    cam.location = loc
    aim = bpy.data.objects.new("aim", None)
    bpy.context.collection.objects.link(aim)
    aim.location = target
    c = cam.constraints.new("TRACK_TO")
    c.target = aim
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"
    cam_data.dof.focus_object = aim
    bpy.context.scene.camera = cam
    return cam, aim


def key(obj, path, frame, value):
    setattr(obj, path, value)
    obj.keyframe_insert(data_path=path, frame=frame)


def smooth_all():
    for action in bpy.data.actions:
        for fc in getattr(action, "fcurves", []):
            for kp in fc.keyframe_points:
                kp.interpolation = "BEZIER"
                kp.easing = "AUTO"


def area_light(loc, rot, size, color, energy):
    light = bpy.data.lights.new("area", "AREA")
    light.size = size
    light.color = color
    light.energy = energy
    o = bpy.data.objects.new("area", light)
    bpy.context.collection.objects.link(o)
    o.location, o.rotation_euler = loc, rot
    return o


# ---------------------------------------------------------------- props
def alpr_unit(at=(0, 0, 0), height=4.2):
    """A generic pole-mounted plate reader: pole, arm, camera housing, lens, solar panel."""
    metal = mat("pole", (0.18, 0.2, 0.21), rough=0.35, metal=0.8)
    housing = mat("housing", (0.03, 0.035, 0.04), rough=0.25, metal=0.2)
    glass = mat("lens", (0.0, 0.0, 0.0), rough=0.03, metal=0.6)
    ring_m = mat("lens_ring", CORAL, emit=(1.0, 0.22, 0.1), strength=5.0)
    pupil_m = mat("pupil", CORAL, emit=(1.0, 0.35, 0.2), strength=2.5)
    panel = mat("panel", (0.02, 0.05, 0.12), rough=0.15, metal=0.4)
    x, y, z = at
    add(bpy.ops.mesh.primitive_cylinder_add, "pole", metal, radius=0.06, depth=height, location=(x, y, z + height / 2))
    add(bpy.ops.mesh.primitive_cube_add, "arm", metal, size=1, location=(x + 0.25, y, z + height - 0.15), scale=(0.55, 0.06, 0.06))
    body = add(bpy.ops.mesh.primitive_cube_add, "housing", housing, size=1,
               location=(x + 0.5, y, z + height - 0.38), scale=(0.34, 0.2, 0.26))
    bpy.ops.object.modifier_add(type="BEVEL")
    body.modifiers["Bevel"].width = 0.05
    body.modifiers["Bevel"].segments = 4
    body.rotation_euler = (0, math.radians(12), 0)
    lens = add(bpy.ops.mesh.primitive_cylinder_add, "lens", glass, radius=0.075, depth=0.05,
               location=(x + 0.5, y - 0.105, z + height - 0.4), rotation=(math.radians(90), 0, 0))
    add(bpy.ops.mesh.primitive_torus_add, "lens_ring", ring_m, major_radius=0.078, minor_radius=0.008,
        location=(x + 0.5, y - 0.132, z + height - 0.4), rotation=(math.radians(90), 0, 0))
    add(bpy.ops.mesh.primitive_uv_sphere_add, "pupil", pupil_m, radius=0.012,
        location=(x + 0.5, y - 0.13, z + height - 0.4))
    solar = add(bpy.ops.mesh.primitive_cube_add, "solar", panel, size=1, location=(x - 0.05, y, z + height + 0.25),
                scale=(0.7, 0.45, 0.02))
    solar.rotation_euler = (math.radians(-25), 0, 0)
    return body, lens


def road(length=60, width=7):
    asphalt = mat("asphalt", (0.018, 0.02, 0.022), rough=0.28, metal=0.1)
    stripe = mat("stripe", (0.9, 0.8, 0.5), emit=AMBER, strength=0.4)
    add(bpy.ops.mesh.primitive_plane_add, "road", asphalt, size=1, location=(0, 0, 0), scale=(width, length, 1))
    for i in range(-12, 13):
        add(bpy.ops.mesh.primitive_plane_add, "dash", stripe, size=1, location=(0, i * 2.4, 0.01), scale=(0.08, 1.0, 1))
    ground = mat("ground", (0.01, 0.03, 0.03), rough=0.9)
    add(bpy.ops.mesh.primitive_plane_add, "ground", ground, size=1, location=(0, 0, -0.02), scale=(200, 200, 1))


def car(color=(0.42, 0.46, 0.5)):
    paint = mat("paint", color, rough=0.2, metal=0.6)
    glass = mat("glass", (0.01, 0.01, 0.015), rough=0.05)
    lamp = mat("lamp", (1, 0.2, 0.1), emit=(1, 0.08, 0.04), strength=12)
    plate = mat("plate", (0.9, 0.9, 0.85), emit=CREAM, strength=1.2)
    head = mat("head", (1, 1, 0.9), emit=(1, 0.95, 0.8), strength=15)
    root = bpy.data.objects.new("car", None)
    bpy.context.collection.objects.link(root)
    parts = [
        add(bpy.ops.mesh.primitive_cube_add, "body", paint, size=1, location=(0, 0, 0.55), scale=(1.8, 4.2, 0.6)),
        add(bpy.ops.mesh.primitive_cube_add, "cabin", glass, size=1, location=(0, 0.2, 1.05), scale=(1.55, 2.0, 0.5)),
        add(bpy.ops.mesh.primitive_cube_add, "tail_l", lamp, size=1, location=(-0.7, -2.11, 0.65), scale=(0.3, 0.03, 0.1)),
        add(bpy.ops.mesh.primitive_cube_add, "tail_r", lamp, size=1, location=(0.7, -2.11, 0.65), scale=(0.3, 0.03, 0.1)),
        add(bpy.ops.mesh.primitive_cube_add, "plate", plate, size=1, location=(0, -2.12, 0.45), scale=(0.5, 0.02, 0.14)),
        add(bpy.ops.mesh.primitive_cube_add, "head_l", head, size=1, location=(-0.65, 2.11, 0.62), scale=(0.3, 0.03, 0.1)),
        add(bpy.ops.mesh.primitive_cube_add, "head_r", head, size=1, location=(0.65, 2.11, 0.62), scale=(0.3, 0.03, 0.1)),
    ]
    for p in parts:
        if p.name.startswith("body") or p.name.startswith("cabin"):
            bpy.context.view_layer.objects.active = p
            bpy.ops.object.modifier_add(type="BEVEL")
            p.modifiers["Bevel"].width = 0.12
            p.modifiers["Bevel"].segments = 4
        p.parent = root
    return root


def scan_cone(apex, length=6.0, angle=18):
    beam = mat("beam", CORAL, emit=CORAL, strength=0.6, alpha=0.07)
    r = length * math.tan(math.radians(angle))
    cone = add(bpy.ops.mesh.primitive_cone_add, "beam", beam, radius1=r, radius2=0.02, depth=length,
               location=(apex[0], apex[1] - length / 2 * 0.7, apex[2] - length / 2 * 0.7))
    cone.rotation_euler = (math.radians(-45), 0, 0)
    return cone


# ---------------------------------------------------------------- scenes
def scene_camera_hero(params):
    """Macro on the glowing lens, pulling back to reveal the pole over a wet coastal road at dusk."""
    s = reset(seconds=params.get("seconds", 4.0))
    backdrop()
    road()
    alpr_unit(at=(1.6, 2.0, 0))
    for i, y in enumerate([-16, 12, 26]):
        alpr_unit(at=(-2.8 if i % 2 else 2.8, y, 0), height=4.0)
    for y in (-10, 4, 18):
        streetlight((-3.5, y, 5.5))
    area_light((6, -4, 7), (math.radians(60), 0, math.radians(40)), 5, AMBER, 500)
    area_light((-6, 6, 4), (math.radians(70), 0, math.radians(-120)), 4, MINT, 250)
    cam, aim = camera((2.4, 0.9, 3.8), (2.1, 1.9, 3.8), lens=50)
    cam.data.dof.aperture_fstop = 1.8
    key(cam, "location", 1, Vector((2.3, 0.95, 3.82)))
    key(cam, "location", s.frame_end, Vector((4.6, -4.8, 2.4)))
    key(aim, "location", 1, Vector((2.1, 1.9, 3.82)))
    key(aim, "location", s.frame_end, Vector((1.6, 3.0, 3.0)))
    smooth_all()


def scene_plate_scan(params):
    s = reset(seconds=params.get("seconds", 4.0))
    backdrop()
    road()
    for y in (-8, 6):
        streetlight((-3.5, y, 5.5))
    _, lens = alpr_unit(at=(2.6, 2.0, 0))
    scan_cone((3.1, 1.9, 3.8))
    c = car()
    key(c, "location", 1, Vector((0.9, 14, 0)))
    key(c, "location", s.frame_end, Vector((0.9, -10, 0)))
    area_light((0, -4, 9), (math.radians(20), 0, 0), 8, AMBER, 600)
    cam, aim = camera((-3.5, -9.0, 1.4), (1.5, 2.0, 1.4), lens=32)
    key(cam, "location", 1, Vector((-3.6, -9.5, 1.3)))
    key(cam, "location", s.frame_end, Vector((-2.8, -7.5, 1.9)))
    smooth_all()
    for fc in c.animation_data.action.fcurves if hasattr(c.animation_data.action, "fcurves") else []:
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"


def _extrude_outline(rings, thickness, material):
    import bmesh
    mesh = bpy.data.meshes.new("county")
    bm = bmesh.new()
    for ring in rings:
        verts = [bm.verts.new((x, y, 0)) for x, y in ring[:-1]]
        if len(verts) >= 3:
            face = bm.faces.new(verts)
            ext = bmesh.ops.extrude_face_region(bm, geom=[face])
            moved = [e for e in ext["geom"] if isinstance(e, bmesh.types.BMVert)]
            bmesh.ops.translate(bm, verts=moved, vec=(0, 0, thickness))
    bm.to_mesh(mesh)
    obj = bpy.data.objects.new("county", mesh)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(material)
    return obj


def scene_county_pins(params):
    """Real county outline with every mapped point rising as a light pin."""
    s = reset(seconds=params.get("seconds", 5.0), samples=12)
    rings, points = params["rings"], params["points"]
    slab = mat("slab", (0.05, 0.25, 0.26), rough=0.6, emit=(0.04, 0.2, 0.21), strength=0.6)
    edge = mat("edge", MINT, emit=MINT, strength=2.0)
    pin_m = mat("pin", CORAL, emit=(1.0, 0.25, 0.12), strength=2.5)
    county = _extrude_outline(rings, 0.25, slab)
    bpy.context.view_layer.objects.active = county
    for ring in rings:
        curve = bpy.data.curves.new("edge", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.035
        sp = curve.splines.new("POLY")
        sp.points.add(len(ring) - 1)
        for i, (x, y) in enumerate(ring):
            sp.points[i].co = (x, y, 0.27, 1)
        o = bpy.data.objects.new("edge", curve)
        o.data.materials.append(edge)
        bpy.context.collection.objects.link(o)
    n = len(points)
    for i, (x, y) in enumerate(points):
        pin = add(bpy.ops.mesh.primitive_cylinder_add, f"pin{i}", pin_m, radius=0.05, depth=1.0, location=(x, y, 0.25))
        start = 10 + int(i / max(1, n) * s.frame_end * 0.55)
        pin.scale = (1, 1, 0.001)
        pin.keyframe_insert("scale", frame=start)
        pin.scale = (1, 1, 1 + (i % 7) * 0.12)
        pin.location.z = 0.25 + 0.5 * pin.scale.z
        pin.keyframe_insert("scale", frame=start + 8)
        pin.keyframe_insert("location", frame=start + 8)
        pin.location.z = 0.25
        pin.keyframe_insert("location", frame=start)
    area_light((0, 0, 30), (0, 0, 0), 40, MINT, 1500)
    cx = sum(p[0] for p in points) / n
    cy = sum(p[1] for p in points) / n
    cam, aim = camera((cx - 2, cy - 16, 13), (cx, cy, 0), lens=30)
    key(cam, "location", 1, Vector((cx - 4, cy - 20, 17)))
    key(cam, "location", s.frame_end, Vector((cx + 1, cy - 11, 8)))
    cam.data.dof.use_dof = False


def scene_network_arcs(params):
    """One local node sending out arcs to many outside nodes: access and sharing."""
    s = reset(seconds=params.get("seconds", 5.0), samples=12)
    count = min(int(params.get("count", 100)), 320)
    node = mat("node", MINT, emit=MINT, strength=6)
    far = mat("far", CREAM, emit=CREAM, strength=1.5)
    arc_m = mat("arc", CORAL, emit=(1.0, 0.3, 0.15), strength=2)
    grid = mat("grid", TEAL, rough=0.8, emit=TEAL, strength=0.2)
    add(bpy.ops.mesh.primitive_plane_add, "floor", grid, size=1, scale=(80, 80, 1))
    add(bpy.ops.mesh.primitive_uv_sphere_add, "home", node, radius=0.6, location=(0, 0, 0.6))
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(count):
        r = 6 + 18 * math.sqrt((i + 0.5) / count)
        a = i * golden
        tx, ty = r * math.cos(a), r * math.sin(a)
        add(bpy.ops.mesh.primitive_uv_sphere_add, f"far{i}", far, radius=0.12, location=(tx, ty, 0.12))
        curve = bpy.data.curves.new(f"arc{i}", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.02
        curve.bevel_factor_mapping_end = "SPLINE"
        sp = curve.splines.new("BEZIER")
        sp.bezier_points.add(1)
        p0, p1 = sp.bezier_points
        h = 2 + r * 0.35
        p0.co, p1.co = (0, 0, 0.6), (tx, ty, 0.12)
        p0.handle_right = (tx * 0.3, ty * 0.3, h)
        p1.handle_left = (tx * 0.7, ty * 0.7, h)
        p0.handle_left, p1.handle_right = p0.co, p1.co
        o = bpy.data.objects.new(f"arc{i}", curve)
        o.data.materials.append(arc_m)
        bpy.context.collection.objects.link(o)
        start = 5 + int(i / count * s.frame_end * 0.6)
        curve.bevel_factor_end = 0.0
        curve.keyframe_insert("bevel_factor_end", frame=start)
        curve.bevel_factor_end = 1.0
        curve.keyframe_insert("bevel_factor_end", frame=start + 18)
    cam, aim = camera((0, -34, 22), (0, 0, 0), lens=28)
    key(cam, "location", 1, Vector((0, -38, 26)))
    key(cam, "location", s.frame_end, Vector((6, -26, 16)))
    cam.data.dof.use_dof = False


def scene_retention_blocks(params):
    """One small cube per day retained; each year is a 15x25 slab and the years stack into a tower."""
    days = int(params.get("days", 365))
    s = reset(seconds=params.get("seconds", 5.0))
    backdrop()
    cube = mat("day", MINT, emit=MINT, strength=0.9, rough=0.4)
    old = mat("old", CORAL, emit=(1.0, 0.3, 0.15), strength=1.2, rough=0.4)
    floor = mat("floor", INK, rough=0.3, metal=0.2)
    add(bpy.ops.mesh.primitive_plane_add, "floor", floor, size=1, scale=(200, 200, 1))
    years = math.ceil(days / 365)
    step = 0.14
    per_year = (s.frame_end * 0.75) / years
    for yr in range(years):
        n = min(365, days - yr * 365)
        m = cube if yr == years - 1 else old
        bpy.ops.mesh.primitive_cube_add(size=0.11, location=(0, 0, 0))
        proto = bpy.context.active_object
        proto.data.materials.append(m)
        objs = []
        for d in range(n):
            col, row = d % 25, d // 25
            o = proto.copy()
            o.location = ((col - 12) * step, (row - 7) * step, 0.07 + yr * 0.16)
            bpy.context.collection.objects.link(o)
            start = 1 + int(yr * per_year + (d / 365) * per_year)
            o.scale = (0.001, 0.001, 0.001)
            o.keyframe_insert("scale", frame=start)
            o.scale = (1, 1, 1)
            o.keyframe_insert("scale", frame=start + 6)
            objs.append(o)
        bpy.data.objects.remove(proto)
    area_light((0, -6, 12), (math.radians(25), 0, 0), 20, CREAM, 500)
    streetlight((3, -3, 3), AMBER, 200)
    top = 0.16 * years
    cam, aim = camera((-4.5, -6.5, 2.0), (0, 0, top * 0.5), lens=35)
    key(cam, "location", 1, Vector((-5.5, -7.5, 1.4)))
    key(cam, "location", s.frame_end, Vector((-2.8, -4.8, top + 1.6)))
    key(aim, "location", 1, Vector((0, 0, 0.2)))
    key(aim, "location", s.frame_end, Vector((0, 0, top * 0.6)))
    smooth_all()


SCENES = {
    "camera_hero": scene_camera_hero,
    "plate_scan": scene_plate_scan,
    "county_pins": scene_county_pins,
    "network_arcs": scene_network_arcs,
    "retention_blocks": scene_retention_blocks,
}


# Render every Nth frame and rebuild the rest with motion-compensated interpolation.
# On these slow, clean CG moves the rebuilt frames are within ~2/255 of true renders,
# and it cuts render time by 2-3x. Fast motion (the passing car) uses a smaller step.
FRAME_STEP = {"plate_scan": 2}


def render(scene, out, params):
    SCENES[scene](params)
    s = bpy.context.scene
    step = int(os.environ.get("SOCIAL_3D_STEP", FRAME_STEP.get(scene, 3)))
    s.frame_step = step
    with tempfile.TemporaryDirectory() as tmp:
        s.render.filepath = os.path.join(tmp, "f_")
        s.render.image_settings.file_format = "PNG"
        bpy.ops.render.render(animation=True)
        done = sorted(Path(tmp).glob("f_*.png"))
        for i, f in enumerate(done):
            f.rename(Path(tmp) / f"k_{i:04d}.png")
        vf = [] if step == 1 else ["-vf", f"minterpolate=fps={s.render.fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"]
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", f"{s.render.fps / step}",
                        "-i", os.path.join(tmp, "k_%04d.png"), *vf, "-c:v", "libx264", "-crf", "16",
                        "-pix_fmt", "yuv420p", str(out)], check=True)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    p = argparse.ArgumentParser()
    p.add_argument("--scene", required=True, choices=sorted(SCENES))
    p.add_argument("--out", required=True)
    p.add_argument("--params", default="{}")
    p.add_argument("--frames", type=int, help="render only this many frames (preview)")
    args = p.parse_args(argv)
    params = json.loads(Path(args.params).read_text()) if args.params.endswith(".json") else json.loads(args.params)
    if args.frames:
        orig = SCENES[args.scene]

        def limited(prm, orig=orig):
            orig(prm)
            bpy.context.scene.frame_end = args.frames
        SCENES[args.scene] = limited
    render(args.scene, args.out, params)


if __name__ == "__main__":
    main()
