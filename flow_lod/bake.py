"""Turn LOD level definitions into real objects in the scene.

The source object is hidden, never modified.
"""

from __future__ import annotations

import math
import time

import bmesh
import bpy

from .analyse import (
    FEATURE, LOCKED, Settings, analyse, detect_symmetry, prepare, protect_vertices, symmetry_report,
    tri_count, quad_ratio,
)
from .simplify import collapse_chords, simplify_to


CURVE_RATIOS = (0.9, 0.7, 0.5, 0.35, 0.25, 0.18, 0.12, 0.08, 0.05, 0.03)


def mesh_deviation(reference_coords, me, diagonal: float) -> float:
    """Mean distance from the reference points to the nearest vertex of `me`, over the diagonal.

    A vertex-to-vertex proxy for surface deviation. It is not a Hausdorff distance, but it is
    monotonic in the thing we care about and costs one KD-tree instead of a mesh raycast per
    point, which keeps a whole curve under a couple of seconds.
    """
    from mathutils.kdtree import KDTree

    coords = [v.co.copy() for v in me.vertices]
    if not coords or not reference_coords:
        return 1.0
    tree = KDTree(len(coords))
    for i, co in enumerate(coords):
        tree.insert(co, i)
    tree.balance()
    total = sum(tree.find(p)[2] for p in reference_coords)
    return (total / len(reference_coords)) / max(1e-12, diagonal)


def error_curve(me, settings: Settings, diagonal: float) -> list:
    """[(ratio, tris, deviation)] measured by actually reducing at each ratio.

    This is the "principled guidance" a fixed ladder lacks: it turns "how many triangles?" into
    "how much error can I accept?", and because deviation at a given ratio differs per model
    (0.0111 / 0.0090 / 0.0074 across three measured hulls at 25%), equalising error rather than
    ratio is what makes a fleet of assets look consistent.
    """
    reference = [v.co.copy() for v in me.vertices]
    tmp = bpy.data.objects.new("_flowlod_curve", me.copy())
    bpy.context.scene.collection.objects.link(tmp)
    out = []
    for ratio in CURVE_RATIOS:
        tmp.modifiers.clear()
        mod = tmp.modifiers.new("c", "DECIMATE")
        mod.ratio = ratio
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = bpy.data.meshes.new_from_object(tmp.evaluated_get(depsgraph))
        tris = sum(len(p.vertices) - 2 for p in evaluated.polygons)
        out.append((ratio, tris, mesh_deviation(reference, evaluated, diagonal)))
        bpy.data.meshes.remove(evaluated)
    mesh = tmp.data
    bpy.data.objects.remove(tmp)
    bpy.data.meshes.remove(mesh)
    return out


def tris_for_deviation(curve: list, max_deviation: float) -> int:
    """Smallest triangle count whose measured deviation stays within `max_deviation`."""
    if not curve:
        return 4
    ok = [c for c in curve if c[2] <= max_deviation]
    if not ok:
        return curve[0][1]              # even the mildest reduction exceeds it; be conservative
    return min(c[1] for c in ok)


def resolve_target(mode: str, value: float, source_tris: int, curve=None) -> int:
    """A budget field means triangles, a fraction of the repaired source, or a deviation cap."""
    if mode == "DEVIATION":
        return max(4, tris_for_deviation(curve or [], value))
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
        "symmetry_drift": symmetry_report(bm, s),
        "uvs_mirrored": uvs_are_mirrored(obj.data),
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


def uvs_are_mirrored(me, axis: int = 0) -> bool:
    """Do both halves of the mesh share UV space, or does each side have its own?

    Symmetrizing replaces one half with a mirror of the other, including its UVs. If each side had
    unique UVs, half the model then samples the wrong part of the texture and any asymmetric
    detail is mirrored. Measured on one hull: 1 shared UV cell of 3,900 before, 100% after.
    """
    if not me.uv_layers:
        return True
    uv = me.uv_layers[0].data
    grid = 256
    left, right = set(), set()
    for poly in me.polygons:
        loops = list(poly.loop_indices)
        centre = sum(me.vertices[v].co[axis] for v in poly.vertices) / len(poly.vertices)
        u = sum(uv[i].uv[0] for i in loops) / len(loops)
        v = sum(uv[i].uv[1] for i in loops) / len(loops)
        (left if centre < 0 else right).add((int(u * grid), int(v * grid)))
    smaller = max(1, min(len(left), len(right)))
    return len(left & right) > 0.5 * smaller


def symmetrize_rebuild(me, axis: str, diagonal: float) -> bool:
    """Exact symmetry that keeps each side's own texture space.

    Cut down the middle, keep the sparser half, mirror it for exact symmetry, then give the
    mirrored half its own region of the UV atlas so both sides can be re-baked from the source
    independently. Unlike a plain symmetrize this does not mirror texture detail -- it only
    mirrors geometry.

    Callers must re-bake afterwards: the UV layout has changed and the old textures no longer
    apply.
    """
    from mathutils import Matrix, Vector

    index = "XYZ".index(axis)
    normal = Vector((1.0 if index == 0 else 0.0,
                     1.0 if index == 1 else 0.0,
                     1.0 if index == 2 else 0.0))

    bm = bmesh.new()
    bm.from_mesh(me)
    uv_layer = bm.loops.layers.uv.active
    if uv_layer is None:
        bm.free()
        return False

    bmesh.ops.bisect_plane(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:],
                           plane_co=Vector((0, 0, 0)), plane_no=normal,
                           clear_inner=False, clear_outer=False)

    def centre(face):
        return sum(v.co[index] for v in face.verts) / len(face.verts)

    negative = [f for f in bm.faces if centre(f) < 0]
    positive = [f for f in bm.faces if centre(f) >= 0]
    if not negative or not positive:
        bm.free()
        return False

    n_verts = len({v for f in negative for v in f.verts})
    p_verts = len({v for f in positive for v in f.verts})
    keep, drop = (negative, positive) if n_verts <= p_verts else (positive, negative)
    bmesh.ops.delete(bm, geom=drop, context="FACES")

    # Halve the kept side's UVs, then hand the mirror the other half of the atlas. Repacking
    # afterwards recovers the texel density this costs.
    for face in bm.faces:
        for loop in face.loops:
            loop[uv_layer].uv.x *= 0.5

    # bmesh.ops.mirror duplicates AND mirrors; duplicating first would create a third copy whose
    # UV offset then lands on geometry that gets welded away.
    result = bmesh.ops.mirror(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:],
                              matrix=Matrix.Identity(4), merge_dist=-1, axis=axis)
    for face in (f for f in result["geom"] if isinstance(f, bmesh.types.BMFace)):
        for loop in face.loops:
            loop[uv_layer].uv.x += 0.5

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=diagonal * 1e-5)
    bm.to_mesh(me)
    bm.free()
    me.update()
    return True


def repack_uvs(obj, margin: float = 0.002):
    """Recover texel density after the atlas split, using Blender's own packer."""
    previous = bpy.context.view_layer.objects.active
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        bpy.ops.uv.pack_islands(margin=margin)
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        return False
    finally:
        bpy.context.view_layer.objects.active = previous
    return True


def symmetrize_mesh(me, axis: str):
    """Mirror one half of the mesh onto the other, giving exact symmetry."""
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.symmetrize(bm, input=bm.verts[:] + bm.edges[:] + bm.faces[:],
                         direction=axis, dist=1e-4)
    bm.to_mesh(me)
    bm.free()
    me.update()


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

    curve = None
    if any(mode == "DEVIATION" for mode, _v in levels):
        probe = bmesh.new()
        probe.from_mesh(obj.data)
        prepare(probe, settings)
        staged = bpy.data.meshes.new("_flowlod_curve_src")
        probe.to_mesh(staged)
        probe.free()
        diag = max(1e-9, obj.dimensions.length)
        curve = error_curve(staged, settings, diag)
        bpy.data.meshes.remove(staged)

    reports = []
    previous = None
    for i, (mode, value) in enumerate(levels, start=1):
        target = resolve_target(mode, value, stats["raw_tris"], curve)
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
        if settings.symmetrize and report.get("symmetry") not in (None, "none"):
            if settings.symmetrize_mode == "REBUILD":
                diag = max(1e-9, obj.dimensions.length)
                ok = symmetrize_rebuild(lod.data, report["symmetry"], diag)
                report["symmetrized"] = "rebuild" if ok else "failed"
                if ok:
                    repack_uvs(lod)
            else:
                symmetrize_mesh(lod.data, report["symmetry"])
                report["symmetrized"] = "mirror"
            # Mirroring a half can yield more triangles than the asymmetric result did, so the
            # level may land over budget. Say so rather than quietly reporting the pre-symmetrize
            # count.
            report["final_tris"] = sum(len(p.vertices) - 2 for p in lod.data.polygons)
            report["final_verts"] = len(lod.data.vertices)
            report["hit_budget"] = report["final_tris"] <= target * 1.02

        if settings.bake_normals:
            img = bake_normal_map(lod, obj, settings, f"{name}_normal")
            report["baked_normal"] = img.name if img else "failed"

        report["name"] = name
        report["below_floor"] = target < stats["structural_floor"]
        reports.append(report)

    obj.hide_set(True)
    obj.hide_render = True
    return stats, reports


def bake_normal_map(low, high, settings: Settings, name: str):
    """Project the source geometry onto the LOD as a tangent-space normal map.

    Blender does high-to-low projection natively -- `bake(use_selected_to_active=True)` with a cage
    and a ray limit. No addon is needed for this, and because the LOD inherits the source UV layout
    the map lands on a usable, non-overlapping unwrap for free.

    Returns the image, or None if baking is unavailable.
    """
    if not low.data.uv_layers:
        return None

    scene = bpy.context.scene
    previous_engine = scene.render.engine
    image = bpy.data.images.new(name, settings.bake_resolution, settings.bake_resolution,
                                alpha=False, float_buffer=False)
    image.colorspace_settings.name = "Non-Color"

    # Every material slot needs the target image as its active node, because the bake writes to
    # whichever image node is active in each material it touches.
    if not low.data.materials:
        low.data.materials.append(bpy.data.materials.new(f"{name}_mat"))

    # The LOD shares its material datablocks with the source. Adding bake nodes to them would
    # edit the SOURCE asset's materials, which is never acceptable -- give the LOD its own copies
    # before touching anything.
    for i, slot_mat in enumerate(low.data.materials):
        if slot_mat is not None:
            own = slot_mat.copy()
            own.name = f"{name}_{slot_mat.name}"
            low.data.materials[i] = own

    targets = []
    for slot_mat in low.data.materials:
        if slot_mat is None:
            continue
        slot_mat.use_nodes = True
        node = slot_mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = image
        node.location = (-900, -400)
        slot_mat.node_tree.nodes.active = node
        targets.append((slot_mat, node))

    extent = max(high.dimensions) or 1.0
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1
    scene.render.bake.use_selected_to_active = True
    scene.render.bake.use_clear = True
    scene.render.bake.margin = settings.bake_margin

    was_hidden, was_hidden_render = low.hide_get(), high.hide_render
    high.hide_render = False
    high.hide_set(False)
    low.hide_set(False)
    bpy.ops.object.select_all(action="DESELECT")
    high.select_set(True)
    low.select_set(True)
    bpy.context.view_layer.objects.active = low

    try:
        bpy.ops.object.bake(
            type="NORMAL", normal_space="TANGENT", use_selected_to_active=True,
            cage_extrusion=extent * settings.cage_factor,
            max_ray_distance=extent * settings.ray_factor,
            margin=settings.bake_margin,
        )
    except Exception:
        for slot_mat, node in targets:
            slot_mat.node_tree.nodes.remove(node)
        bpy.data.images.remove(image)
        scene.render.engine = previous_engine
        low.hide_set(was_hidden)
        high.hide_render = was_hidden_render
        return None

    # Wire it in so the LOD actually renders and exports with the detail, rather than the bake
    # being an orphan image nobody references.
    for slot_mat, node in targets:
        tree = slot_mat.node_tree
        normal_map = tree.nodes.new("ShaderNodeNormalMap")
        normal_map.location = (-600, -400)
        tree.links.new(node.outputs["Color"], normal_map.inputs["Color"])
        principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if principled is not None:
            # The bake already contains the source's own normal map -- Blender bakes shading
            # normals, not just geometry -- so the previous chain must be disconnected or the
            # detail would be applied twice.
            for link in list(principled.inputs["Normal"].links):
                tree.links.remove(link)
            tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])

    scene.render.engine = previous_engine
    low.hide_set(was_hidden)
    high.hide_render = was_hidden_render
    return image


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
            f"{('normal=' + r['baked_normal'] + ', ') if r.get('baked_normal') else ''}"
            f"{'symmetrized, ' if r.get('symmetrized') else ''}"
            f"{r['seconds']:.1f}s{flag}"
        )
    return "\n".join(lines)
