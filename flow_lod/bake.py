"""Turn LOD level definitions into real objects in the scene.

The source object is hidden, never modified.
"""

from __future__ import annotations

import math
import time

import bmesh
import bpy

from .analyse import FEATURE, LOCKED, Settings, analyse, prepare, tri_count, quad_ratio
from .simplify import simplify_to


def resolve_target(mode: str, value: float, source_tris: int) -> int:
    """A budget field means triangles, or a fraction of the repaired source."""
    if mode == "QUALITY":
        return max(4, int(source_tris * value))
    return max(4, int(value))


def source_stats(obj) -> dict:
    """Pre-bake numbers for the panel, including what welding would save."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    raw_v, raw_e = len(bm.verts), len(bm.edges)
    split = sum(1 for e in bm.edges if len(e.link_faces) != 2)
    tris = tri_count(bm)

    s = Settings()
    prepare(bm, s)
    an = analyse(bm, s)
    out = {
        "raw_verts": raw_v,
        "raw_edges": raw_e,
        "raw_tris": tris,
        "split_edges": split,
        "welded_verts": len(bm.verts),
        "verts_saved_pct": 100.0 * (raw_v - len(bm.verts)) / max(1, raw_v),
        "quad_ratio": an.stats["quad_ratio"],
        "feature_edges": an.stats["feature"],
        "locked_edges": an.stats["locked"],
        "structural_floor": an.stats["structural_floor"],
        "chord_max": an.stats["chord_max"],
    }
    bm.free()
    return out


def _mark_sharp(bm, settings: Settings):
    """Re-derive sharp edges from the final mesh's own structure.

    Measured on the reference hulls, custom split normals deviate 1.57 degrees mean from face normals --
    they encode 'this model is faceted' and nothing else. Re-marking reproduces the shading without
    a Data Transfer pass. See DESIGN.md section 2.3.
    """
    feat = math.radians(settings.feature_angle)
    for e in bm.edges:
        if len(e.link_faces) != 2 or e.calc_face_angle(0.0) > feat:
            e.smooth = False


def bake_level(obj, target: int, settings: Settings, name: str):
    """Build one LOD object. Returns (object, report)."""
    t0 = time.time()

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    was_quad = quad_ratio(bm) >= settings.quad_skip_ratio
    prep = prepare(bm, settings)
    report = simplify_to(bm, settings, target, source_was_quad_dominant=was_quad)

    bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 3])
    if settings.remark_sharp:
        _mark_sharp(bm, settings)

    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()

    lod = bpy.data.objects.new(name, me)
    lod.matrix_world = obj.matrix_world.copy()
    for slot in obj.material_slots:
        me.materials.append(slot.material)

    report["prepare"] = prep
    report["seconds"] = time.time() - t0
    return lod, report


def bake(obj, settings: Settings, levels) -> list:
    """Bake every level into a <Name>_LODs collection. `levels` is a list of (mode, value)."""
    stats = source_stats(obj)
    coll_name = f"{obj.name}_LODs"
    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        coll = bpy.data.collections.new(coll_name)
        parent = obj.users_collection[0] if obj.users_collection else bpy.context.scene.collection
        parent.children.link(coll)

    # clear a previous bake so re-baking is idempotent rather than accumulating duplicates
    for old in list(coll.objects):
        coll.objects.unlink(old)
        if old.users == 0:
            bpy.data.objects.remove(old)

    reports = []
    for i, (mode, value) in enumerate(levels, start=1):
        target = resolve_target(mode, value, stats["raw_tris"])
        name = f"{obj.name}_LOD{i}"
        lod, report = bake_level(obj, target, settings, name)
        coll.objects.link(lod)

        if settings.remark_sharp:
            prev = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = lod
            try:
                bpy.ops.object.shade_auto_smooth()
            except Exception:
                pass
            bpy.context.view_layer.objects.active = prev

        if settings.transfer_normals:
            mod = lod.modifiers.new("FlowLOD Normals", "DATA_TRANSFER")
            mod.object = obj
            mod.use_loop_data = True
            mod.data_types_loops = {"CUSTOM_NORMAL"}
            mod.loop_mapping = "POLYINTERP_NEAREST"

        report["name"] = name
        report["below_floor"] = target < stats["structural_floor"]
        reports.append(report)

    obj.hide_set(True)
    obj.hide_render = True
    return stats, reports


def format_report(stats: dict, reports: list) -> str:
    lines = [
        f"source: {stats['raw_tris']} tris, {stats['raw_verts']} verts "
        f"({stats['split_edges']} split edges)",
        f"welded: {stats['welded_verts']} verts "
        f"(-{stats['verts_saved_pct']:.0f}%), quads {stats['quad_ratio']:.0%}, "
        f"floor ~{stats['structural_floor']} tris",
    ]
    for r in reports:
        flag = ""
        if not r["hit_budget"]:
            flag = f"  MISSED (target {r['target']})"
        elif r["below_floor"]:
            flag = "  below structural floor"
        lines.append(
            f"  {r['name']}: {r['final_tris']} tris, {r['final_verts']} verts, "
            f"quads {r['quad_ratio']:.0%}, tier={r['deepest_tier_name']}, "
            f"chords={'on' if r.get('chords_used') else 'off'}, "
            f"{r['seconds']:.1f}s{flag}"
        )
    return "\n".join(lines)
