"""Turn LOD level definitions into real objects in the scene.

The source object is hidden, never modified.
"""

from __future__ import annotations

import math
import time

import bmesh
import bpy

from .analyse import (
    FEATURE, LOCKED, Settings, analyse, detect_symmetry, prepare, protect_vertices, tri_count,
    quad_ratio,
)
from .simplify import collapse_chords, simplify_to


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
        "symmetry": detect_symmetry(bm, s) or "none",
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


def _needs_no_prep(me, settings: Settings) -> bool:
    """True when every preprocessing stage would be a no-op for this mesh and these settings."""
    if settings.detriangulate or settings.use_chords == "ALWAYS":
        return False
    if settings.protect_weight > 0.0 or settings.remark_sharp:
        return False
    if any(len(p.vertices) > 3 for p in me.polygons):
        return False
    if settings.weld:
        import bmesh as _bm
        from .analyse import bbox_diagonal, has_duplicate_verts
        probe = _bm.new()
        probe.from_mesh(me)
        dirty = has_duplicate_verts(probe, settings.weld_factor * bbox_diagonal(probe))
        probe.free()
        if dirty:
            return False
    return True


def mesh_symmetry(me, settings: Settings):
    """Detect the mirror axis of a mesh datablock, or None."""
    probe = bmesh.new()
    probe.from_mesh(me)
    axis = detect_symmetry(probe, settings)
    probe.free()
    return axis


def decimate_mesh(me, target: int, protect_idx, settings: Settings, axis=None) -> tuple:
    """Reduce a mesh datablock to `target` triangles with Blender's Decimate modifier.

    The ratio is iterated because a protect group makes the achieved count undershoot the
    requested one. Four passes is ample -- it converges in two on every mesh measured.

    invert_vertex_group is REQUIRED: Blender reads a weight of 1 as "decimate here", not
    "protect here". Without the inversion the modifier stalls far above the target.
    """
    tmp = bpy.data.objects.new("_flowlod_tmp", me)
    bpy.context.scene.collection.objects.link(tmp)
    if protect_idx and settings.protect_weight > 0.0:
        vg = tmp.vertex_groups.new(name="FlowLOD_Protect")
        vg.add(list(protect_idx), 1.0, "REPLACE")

    # Symmetry constrains which edges may collapse, so the achieved count undershoots slightly.
    # Under budget is fine; the iteration below only corrects overshoot.
    current = sum(len(p.vertices) - 2 for p in me.polygons)
    ratio = min(1.0, target / max(1, current))
    result, achieved, passes = None, current, 0

    for passes in range(1, 5):
        tmp.modifiers.clear()
        mod = tmp.modifiers.new("FlowLOD", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = max(1e-6, min(1.0, ratio))
        mod.use_collapse_triangulate = True
        if axis:
            # Enforced by Blender itself; a symmetric source stays symmetric to machine precision
            # instead of drifting by up to 2.4% of the model's size.
            mod.use_symmetry = True
            mod.symmetry_axis = axis
        if protect_idx and settings.protect_weight > 0.0:
            mod.vertex_group = "FlowLOD_Protect"
            mod.vertex_group_factor = settings.protect_weight
            mod.invert_vertex_group = True

        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = bpy.data.meshes.new_from_object(tmp.evaluated_get(depsgraph))
        achieved = sum(len(p.vertices) - 2 for p in evaluated.polygons)
        if result is not None:
            bpy.data.meshes.remove(result)
        result = evaluated
        if achieved <= target * 1.02:
            break
        ratio *= (target / max(1, achieved)) * 0.98

    bpy.data.objects.remove(tmp)
    return result, {"engine": "decimate", "passes": passes, "achieved": achieved}


def bake_level(obj, target: int, settings: Settings, name: str, source_mesh=None):
    """Build one LOD object. Returns (object, report)."""
    t0 = time.time()

    base = source_mesh if source_mesh is not None else obj.data

    # Fast path: when nothing needs preprocessing, hand the untouched mesh straight to Decimate.
    # A bmesh round trip is not free -- measured, it costs several F1 points on an already-clean
    # mesh -- so the cheapest correct thing is to not do one.
    if settings.engine == "DECIMATE" and _needs_no_prep(base, settings):
        axis = mesh_symmetry(base, settings)
        me, dec = decimate_mesh(base.copy(), target, None, settings, axis=axis)
        dec["symmetry"] = axis or "none"
        me.name = name
        lod = bpy.data.objects.new(name, me)
        lod.matrix_world = obj.matrix_world.copy()
        for slot in obj.material_slots:
            me.materials.append(slot.material)
        final = sum(len(p.vertices) - 2 for p in me.polygons)
        return lod, {
            "target": target, "start_tris": sum(len(p.vertices) - 2 for p in base.polygons),
            "tiers": [("decimate-direct", dec)], "chords_used": False, "protected": 0,
            "deepest_tier": 0, "deepest_tier_name": "decimate-direct",
            "final_tris": final, "final_verts": len(me.vertices), "quad_ratio": 0.0,
            "hit_budget": final <= target * 1.02, "prepare": {"skipped": True},
            "seconds": time.time() - t0,
        }

    bm = bmesh.new()
    bm.from_mesh(base)
    was_quad = quad_ratio(bm) >= settings.quad_skip_ratio
    prep = prepare(bm, settings)

    if settings.engine == "PYTHON":
        report = simplify_to(bm, settings, target, source_was_quad_dominant=was_quad)
        bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 3])
        if settings.remark_sharp:
            _mark_sharp(bm, settings)
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
    else:
        # Optional structured pass first: whole quad rows are the most modeller-like reduction
        # available, and they are nearly free. Decimate finishes the job.
        report = {"target": target, "start_tris": tri_count(bm), "tiers": []}
        chords_on = settings.use_chords == "ALWAYS" or (
            settings.use_chords == "AUTO" and was_quad
        )
        report["chords_used"] = chords_on
        if chords_on:
            floor = max(target, int(report["start_tris"] * settings.chord_floor))
            if tri_count(bm) > floor:
                r = collapse_chords(bm, settings, floor, whole_only=False)
                if r["rows_collapsed"]:
                    report["tiers"].append(("chord-segment", r))

        an = analyse(bm, settings)
        protect = protect_vertices(bm, an.edge_class, settings)
        bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 3])
        if settings.remark_sharp:
            _mark_sharp(bm, settings)
        staged = bpy.data.meshes.new(name + "_stage")
        bm.to_mesh(staged)
        bm.free()

        axis = mesh_symmetry(staged, settings)
        me, dec = decimate_mesh(staged, target, protect, settings, axis=axis)
        dec["symmetry"] = axis or "none"
        me.name = name
        bpy.data.meshes.remove(staged)
        report["tiers"].append(("decimate", dec))
        report["protected"] = len(protect)
        report["deepest_tier"] = 0
        report["deepest_tier_name"] = "decimate"
        report["final_tris"] = sum(len(p.vertices) - 2 for p in me.polygons)
        report["final_verts"] = len(me.vertices)
        report["quad_ratio"] = 0.0
        report["hit_budget"] = report["final_tris"] <= target * 1.02

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
    previous = None
    for i, (mode, value) in enumerate(levels, start=1):
        target = resolve_target(mode, value, stats["raw_tris"])
        name = f"{obj.name}_LOD{i}"
        # Cascade: reduce the previous level rather than the source. Cheaper, and each level is
        # a strict subset of the one above it, so the ladder stays consistent as it descends.
        source_mesh = previous if (settings.cascade and previous is not None) else None
        lod, report = bake_level(obj, target, settings, name, source_mesh=source_mesh)
        previous = lod.data
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

        report["symmetry"] = next(
            (d.get("symmetry") for _n, d in report.get("tiers", []) if isinstance(d, dict)
             and "symmetry" in d), "none")
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
            f"sym={r.get('symmetry', 'none')}, "
            f"{r['seconds']:.1f}s{flag}"
        )
    return "\n".join(lines)
