"""Repair, quad recovery and flow analysis.

Pure bmesh operations, no bpy UI dependencies, so this module is testable headless.

The pipeline here answers one question: *which edges did the modeller mean?*  Everything
downstream is a consequence of that classification.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import bmesh

# Edge classes
FREE = 0
FEATURE = 1
LOCKED = 2

# Vertex classes
V_FREE = 0
V_FEATURE = 1
V_CORNER = 2

UV_EPS = 1e-5


@dataclass
class Settings:
    """Everything the analyser and simplifier are allowed to be opinionated about."""

    # Every preprocessing step costs fidelity and most meshes do not need any of them.
    # Measured on an already-clean hull, preprocessing alone scored F1 94.2% against the raw
    # source with ZERO reduction applied -- 6 points given away before any work was done.
    # So each one is now opt-in or auto-detected, and the analyser says when it would help.
    weld: bool = True                  # auto-skipped when the mesh has no duplicate vertices
    weld_factor: float = 1e-5          # relative to bounding-box diagonal
    detriangulate: bool = False        # costs fidelity; turn on when you want quads or chords

    # A FIXED angle cannot serve two asset classes. Measured, share of edges above a threshold:
    #
    #                     >=30deg  >=50deg  >=70deg
    #   faceted hull        ~60%     42%      31%
    #   smooth hull          22%     14%       6%
    #
    # 70deg protects 31% of the faceted hull and 6% of the smooth one -- on the latter the
    # simplifier runs essentially unconstrained and produces slivers. So the threshold is derived
    # from each mesh's own distribution by default; feature_angle is only the manual override.
    adaptive_features: bool = True
    feature_percentile: float = 25.0   # protect the sharpest quarter of edges
    feature_angle_min: float = 15.0    # clamp, so a flat mesh does not protect noise
    feature_angle_max: float = 80.0    # and a faceted one does not freeze solid
    feature_angle: float = 70.0        # used when adaptive_features is off

    # corner_angle barely moves the result (F1 83.1% at 45deg vs 83.8% at 180deg), so it is wide
    # by default and exists as an escape hatch rather than a tuning knob.
    corner_angle: float = 180.0

    # Chord collapse is OFF for triangulated input by default. Measured on the reference asset at
    # 3057 tris: chords on = F1 78.8%, chords off = F1 83-85%. Removing whole quad rows is
    # density-uniform, so on a hull whose curvature varies it deviates more than error-driven
    # collapse does. It is still the right tool for genuinely quad-modelled assets, where even
    # topology and loop structure are the point -- hence "auto".
    use_chords: str = "AUTO"           # AUTO | ALWAYS | NEVER

    # Chord collapse removes whole quad rows, which is density-uniform. Pushed far enough it
    # eats every row a shape needs and the silhouette collapses -- measured on a subdivided test
    # asset, a 10% budget reached via chords alone flattened the form entirely. Hand over to
    # error-driven collapse below this fraction of the repaired source, where preserving the
    # shape matters more than preserving the loops.
    chord_floor: float = 0.25

    protect_seams: bool = True
    protect_sharp: bool = True
    protect_materials: bool = True
    protect_boundary: bool = True
    protect_curvature: bool = True

    quad_skip_ratio: float = 0.6       # already quad-dominant -> don't bother de-triangulating
    min_segment: int = 3               # tier 2: shortest chord run worth dissolving

    # Protect only junctions where feature lines meet plus hard boundaries (~8% of verts), not
    # the interior of every crease (~52%). Measured: at 52% protected, Blender's Decimate stalls
    # at 4516 tris against a 4084 target and cannot reach any aggressive budget at all.
    selective_protect: bool = True

    # Reduction engine. Blender's Decimate is optimal-position QEM in C and beats this addon's
    # own pure-Python half-edge implementation by 12-17 F1 points at every budget while running
    # instantly (0.0s vs 5-9s) and always hitting the target. Measured on a smooth reference
    # hull against the raw source:
    #
    #             50%    25%    10%
    #   Decimate  94.4%  87.5%  78.6%
    #   PYTHON    82.2%  71.6%  61.5%   (and missed the 10% budget entirely)
    #
    # ponytail: do not reimplement, worse, what is already in the box. PYTHON is kept only so
    # the comparison stays reproducible.
    engine: str = "DECIMATE"           # DECIMATE | PYTHON

    # Measured: protection does not help and at aggressive budgets it stops the target being
    # reached at all (1684 tris against an 816 target). Off unless asked for.
    protect_weight: float = 0.0
    # Cascading re-runs preprocessing on each level, compounding its loss all the way down.
    cascade: bool = False

    # Symmetry. A symmetric model that comes back asymmetric is an obvious, visible defect, and
    # Decimate can enforce mirror symmetry directly. Measured on a symmetric hull at a 25% budget:
    # without it the worst vertex drifts 2.4e-2 of the bounding diagonal; with it, 1.0e-10.
    # AUTO detects the mirror plane from the mesh itself so this needs no thought.
    # Blender's Decimate use_symmetry makes the COLLAPSE PATTERN symmetric. It does not make an
    # approximately-symmetric mesh exact: measured, a perfectly symmetrized input (7.5e-13) still
    # came out at 1.9e-4 after a symmetric decimation. Only symmetrizing the OUTPUT gives exact
    # symmetry (9.8e-12), which is what `symmetrize` does.
    symmetry: str = "AUTO"             # AUTO | X | Y | Z | NONE
    # Detection runs on the MEAN, because that is what signals intent: a hull measured 9.8e-05
    # mean on X (clearly modelled symmetric) against 1.9e-02 on Y and Z (clearly not). Its WORST
    # vertex was 2.1e-02 off, so judging on the worst case would have rejected a mesh that is
    # plainly meant to be symmetric. The worst figure is reported instead, as drift.
    symmetry_tolerance: float = 1e-3   # on the MEAN, relative to bbox diagonal
    symmetrize: bool = False           # mirror each LOD exactly; see the UV warning

    # Normal-map baking. Decimation preserves the source UV layout almost exactly (measured: UV
    # area 0.6521 -> 0.6472, no degenerate or NaN coordinates), so a LOD can reuse the original
    # textures unchanged. The only thing worth rebaking is the geometry that was removed, captured
    # as a tangent-space normal map projected from the source.
    bake_normals: bool = False
    bake_resolution: int = 1024
    bake_margin: int = 8               # pixels bled outside each island, stops edge striping
    cage_factor: float = 0.02          # of the largest dimension
    ray_factor: float = 0.05

    remark_sharp: bool = False         # re-derive sharp edges instead of transferring normals
    transfer_normals: bool = False     # real custom-normal transfer, for meshes where 2.3 fails


@dataclass
class Analysis:
    edge_class: dict = field(default_factory=dict)
    vert_class: dict = field(default_factory=dict)
    polylines: list = field(default_factory=list)
    chords: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def locked_count(self) -> int:
        return sum(1 for c in self.edge_class.values() if c == LOCKED)

    def feature_count(self) -> int:
        return sum(1 for c in self.edge_class.values() if c == FEATURE)

    def structural_floor(self) -> int:
        """Roughly the triangle count below which this mesh's own hard edges cannot all survive.

        In a triangle mesh E ~= 3F/2, so F ~= 2E/3 edges' worth of triangles. Counting only the
        edges that must survive gives a lower bound on the achievable triangle count.

        It is an estimate, not a promise -- it exists so the UI can flag an impossible budget
        before baking rather than after. ponytail: an exact answer would need the actual
        simplification run, which is the thing we are trying to warn about in advance.
        """
        return (2 * (self.locked_count() + self.feature_count())) // 3


def tri_count(bm) -> int:
    """Triangles the mesh will cost once triangulated, which is what a budget actually means."""
    return sum(len(f.verts) - 2 for f in bm.faces)


def quad_ratio(bm) -> float:
    if not bm.faces:
        return 0.0
    return sum(1 for f in bm.faces if len(f.verts) == 4) / len(bm.faces)


def bbox_diagonal(bm) -> float:
    if not bm.verts:
        return 1.0
    xs = [v.co for v in bm.verts]
    lo = [min(c[i] for c in xs) for i in range(3)]
    hi = [max(c[i] for c in xs) for i in range(3)]
    return max(1e-9, math.dist(lo, hi))


# --------------------------------------------------------------------------------------
# Stage 0: repair
# --------------------------------------------------------------------------------------

def has_duplicate_verts(bm, dist: float) -> bool:
    """Would welding at `dist` actually merge anything?

    ponytail: this was a spatial hash, which produced false positives and sent clean meshes down
    the slow path for no reason. A trial weld on a throwaway copy is exact, simpler, and fast
    enough -- the clever version was both wrong and unnecessary.
    """
    if dist <= 0.0:
        return False
    probe = bm.copy()
    before = len(probe.verts)
    bmesh.ops.remove_doubles(probe, verts=probe.verts[:], dist=dist)
    after = len(probe.verts)
    probe.free()
    return after < before


def symmetry_error(bm, axis: int) -> float:
    """(mean, worst) distance from each vertex to the nearest vertex of the mesh mirrored on `axis`.

    Zero for a perfectly symmetric mesh. Normalised by the bounding diagonal by the caller.
    """
    from mathutils.kdtree import KDTree

    verts = [v.co.copy() for v in bm.verts]
    if not verts:
        return float("inf")
    tree = KDTree(len(verts))
    for i, co in enumerate(verts):
        tree.insert(co, i)
    tree.balance()

    # Mean alone is misleading: a hull measured 9.8e-05 mean while its WORST vertex was off by
    # 2.1e-02, two percent of the model. Judge on the worst case.
    total, worst = 0.0, 0.0
    for co in verts:
        mirrored = co.copy()
        mirrored[axis] = -mirrored[axis]
        _, _, dist = tree.find(mirrored)
        total += dist
        worst = max(worst, dist)
    return total / len(verts), worst


def detect_symmetry(bm, settings: Settings):
    """The axis this mesh is mirror-symmetric about, as 'X'/'Y'/'Z', or None.

    Only ever detects symmetry about the object's own origin, which is where Blender's Decimate
    enforces it. A model mirrored about some other plane is not something this can help with.
    """
    if settings.symmetry == "NONE":
        return None
    if settings.symmetry in ("X", "Y", "Z"):
        return settings.symmetry
    if not bm.verts:
        return None

    limit = settings.symmetry_tolerance * bbox_diagonal(bm)
    best, best_err = None, limit
    for axis, name in enumerate("XYZ"):
        mean, _worst = symmetry_error(bm, axis)
        if mean < best_err:
            best, best_err = name, mean
    return best


def symmetry_report(bm, settings: Settings) -> dict:
    """Detected axis plus how far the mesh has actually drifted from symmetry."""
    axis = detect_symmetry(bm, settings)
    out = {"axis": axis or "none", "mean": 0.0, "worst": 0.0}
    if axis:
        diag = bbox_diagonal(bm)
        mean, worst = symmetry_error(bm, "XYZ".index(axis))
        out["mean"], out["worst"] = mean / diag, worst / diag
    return out


def repair(bm, settings: Settings) -> dict:
    """Weld split vertices back into a manifold mesh.

    Exported meshes arrive with every vertex split for shading -- the target reference asset has 13,778
    verts for 6,114 tris, where a manifold equivalent has ~3,057.  Nothing downstream can work on
    that: there is no edge flow because there are no shared edges.

    UVs are safe by construction. They live on loops, not vertices, so merging two verts that
    carried different UVs yields one vert with two differing loops -- which *is* a UV seam, and is
    detected as one in classify_edges().
    """
    before_v, before_e = len(bm.verts), len(bm.edges)
    if not settings.weld:
        return {"welded": False, "verts_before": before_v, "verts_after": before_v}

    dist = settings.weld_factor * bbox_diagonal(bm)
    if not has_duplicate_verts(bm, dist):
        return {"welded": False, "reason": "already clean",
                "verts_before": before_v, "verts_after": before_v, "verts_saved_pct": 0.0}
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=dist)

    return {
        "welded": True,
        "weld_dist": dist,
        "verts_before": before_v,
        "verts_after": len(bm.verts),
        "edges_before": before_e,
        "edges_after": len(bm.edges),
        "verts_saved_pct": 100.0 * (before_v - len(bm.verts)) / max(1, before_v),
    }


# --------------------------------------------------------------------------------------
# Stage 1: quad recovery
# --------------------------------------------------------------------------------------

def detriangulate(bm, settings: Settings) -> dict:
    """Recover the quad topology a triangulated export threw away.

    Quad recovery is a *topology* problem, not a shape problem. Measured on the target reference asset:
    60deg thresholds recover 69.4% quads with mean chord 5.7; wide-open 180deg thresholds recover
    86.9% with mean chord 8.2 and chords up to 55 edges.  Blender's default shape heuristics
    prefer square-looking pairings over the modeller's original ones and fragment the flow.

    The cmp_* comparisons stay on: they cost ~4% of recovery and guarantee no pair is ever merged
    across a seam, a material boundary or a marked-sharp edge.
    """
    before = quad_ratio(bm)
    if not settings.detriangulate or before >= settings.quad_skip_ratio:
        return {"detriangulated": False, "quad_ratio": before}

    wide = math.radians(180.0)
    bmesh.ops.join_triangles(
        bm,
        faces=bm.faces[:],
        angle_face_threshold=wide,
        angle_shape_threshold=wide,
        cmp_seam=settings.protect_seams,
        cmp_sharp=settings.protect_sharp,
        cmp_uvs=True,
        cmp_materials=settings.protect_materials,
    )
    return {
        "detriangulated": True,
        "quad_ratio_before": before,
        "quad_ratio": quad_ratio(bm),
        "quads": sum(1 for f in bm.faces if len(f.verts) == 4),
        "tris": sum(1 for f in bm.faces if len(f.verts) == 3),
    }


# --------------------------------------------------------------------------------------
# Stage 2: classification
# --------------------------------------------------------------------------------------

def _uv_discontinuous(edge, uv_layer) -> bool:
    """True if the two faces across this edge disagree on UVs, i.e. it is a UV island border."""
    if uv_layer is None or len(edge.link_faces) != 2:
        return False
    loops = [l for l in edge.link_loops]
    if len(loops) != 2:
        return False
    a, b = loops
    # compare both endpoints: a-loop start vs the b-loop that shares that vertex
    for la in (a, a.link_loop_next):
        lb = next((x for x in (b, b.link_loop_next) if x.vert is la.vert), None)
        if lb is None:
            continue
        ua, ub = la[uv_layer].uv, lb[uv_layer].uv
        if abs(ua[0] - ub[0]) > UV_EPS or abs(ua[1] - ub[1]) > UV_EPS:
            return True
    return False


def resolve_feature_angle(bm, settings: Settings) -> float:
    """The dihedral threshold to use for THIS mesh, in degrees.

    Adaptive by default: take the angle at the given percentile of the mesh's own manifold-edge
    dihedral distribution, clamped. A faceted hull lands near 55deg and a smooth one near 27deg,
    which is what each actually needs -- a single fixed number serves neither.
    """
    if not settings.adaptive_features:
        return settings.feature_angle

    angles = [math.degrees(e.calc_face_angle(0.0))
              for e in bm.edges if len(e.link_faces) == 2]
    if not angles:
        return settings.feature_angle
    angles.sort()
    idx = int(len(angles) * (1.0 - settings.feature_percentile / 100.0))
    idx = min(max(idx, 0), len(angles) - 1)
    return min(max(angles[idx], settings.feature_angle_min), settings.feature_angle_max)


def classify_edges(bm, settings: Settings) -> dict:
    """LOCKED / FEATURE / FREE per edge.

    LOCKED is everything the user declared by hand plus everything topologically dangerous.
    FEATURE is what we infer from curvature. FREE is fair game.
    """
    uv_layer = bm.loops.layers.uv.active
    crease = bm.edges.layers.float.get("crease_edge")
    bevel = bm.edges.layers.float.get("bevel_weight_edge")
    feat_rad = math.radians(resolve_feature_angle(bm, settings))

    out = {}
    for e in bm.edges:
        nf = len(e.link_faces)

        if settings.protect_boundary and nf != 2:
            out[e] = LOCKED
            continue
        if nf != 2:                                   # non-manifold is never safe regardless
            out[e] = LOCKED
            continue
        if settings.protect_sharp and not e.smooth:
            out[e] = LOCKED
            continue
        if settings.protect_seams and (e.seam or _uv_discontinuous(e, uv_layer)):
            out[e] = LOCKED
            continue
        if settings.protect_materials and \
                e.link_faces[0].material_index != e.link_faces[1].material_index:
            out[e] = LOCKED
            continue
        if crease is not None and e[crease] > 0.0:
            out[e] = LOCKED
            continue
        if bevel is not None and e[bevel] > 0.0:
            out[e] = LOCKED
            continue

        if settings.protect_curvature and e.calc_face_angle(0.0) > feat_rad:
            out[e] = FEATURE
            continue

        out[e] = FREE
    return out


def feature_polylines(bm, edge_class: dict) -> list:
    """Chain FEATURE edges into runs that behave as one curve.

    A crease is allowed to lose vertices along its length. It is not allowed to stop being a
    continuous, straight, sharp line -- so the polyline, not the edge, is the preserved object.
    """
    feats = {e for e, c in edge_class.items() if c == FEATURE}
    seen = set()
    lines = []

    for start in feats:
        if start in seen:
            continue
        line = [start]
        seen.add(start)
        # extend from both ends through vertices where exactly two feature edges meet
        for end in (0, 1):
            cur, vert = start, start.verts[end]
            while True:
                nxt = [e for e in vert.link_edges if e in feats and e is not cur]
                if len(nxt) != 1:
                    break
                cur = nxt[0]
                if cur in seen:
                    break
                seen.add(cur)
                line.insert(0, cur) if end == 0 else line.append(cur)
                vert = cur.other_vert(vert)
        lines.append(line)
    return lines


def protect_vertices(bm, edge_class: dict, settings: Settings) -> list:
    """Vertex indices worth freezing, as a list suitable for a vertex group.

    Selective by default: a vertex qualifies only if it touches a LOCKED edge (a real boundary,
    seam or material break) or is a junction where three or more feature lines meet. The interior
    of a crease is deliberately NOT protected -- a crease may lose vertices along its length
    without ceasing to be a crease, and protecting them all freezes half the mesh.
    """
    out = []
    for v in bm.verts:
        incident = list(v.link_edges)
        if any(edge_class.get(e) == LOCKED for e in incident):
            out.append(v.index)
            continue
        if not settings.selective_protect:
            if any(edge_class.get(e) == FEATURE for e in incident):
                out.append(v.index)
            continue
        if sum(1 for e in incident if edge_class.get(e) == FEATURE) >= 3:
            out.append(v.index)
    return out


def classify_verts(bm, edge_class: dict, settings: Settings) -> dict:
    """CORNER / FEATURE / FREE per vertex.

    CORNER vertices are frozen: where lines meet and where lines turn. Their removal is the kind
    of change you notice instantly.
    """
    corner_rad = math.radians(settings.corner_angle)
    out = {}
    for v in bm.verts:
        incident = [e for e in v.link_edges]
        if any(edge_class.get(e) == LOCKED for e in incident):
            out[v] = V_CORNER
            continue

        feats = [e for e in incident if edge_class.get(e) == FEATURE]
        if not feats:
            out[v] = V_FREE
            continue
        if len(feats) != 2:
            out[v] = V_CORNER          # junction or dead end
            continue

        a, b = feats
        va = (a.other_vert(v).co - v.co).normalized()
        vb = (b.other_vert(v).co - v.co).normalized()
        # straight through => va and vb are near-opposite => angle between them near pi
        turn = math.pi - va.angle(vb, 0.0)
        out[v] = V_CORNER if turn > corner_rad else V_FEATURE
    return out


# --------------------------------------------------------------------------------------
# Stage 2b: the chord graph
# --------------------------------------------------------------------------------------

def _opposite_edge(loop):
    """In a quad, the edge across from this loop's edge."""
    return loop.link_loop_next.link_loop_next.edge


def trace_chords(bm) -> list:
    """Every quad chord, as lists of BMEdge references.

    ponytail: references, never indices. bmesh.ops.dissolve_edges invalidates all indices, and an
    index-based version of this silently dissolved one chord per pass instead of hundreds.
    """
    seen = set()
    chords = []
    for seed in bm.edges:
        if seed in seen or not any(len(f.verts) == 4 for f in seed.link_faces):
            continue
        local = {}
        frontier = [seed]
        while frontier:
            cur = frontier.pop()
            if cur in local:
                continue
            local[cur] = True
            for loop in cur.link_loops:
                if len(loop.face.verts) != 4:
                    continue
                nxt = _opposite_edge(loop)
                if nxt not in local:
                    frontier.append(nxt)
        seen |= set(local)
        chords.append(list(local))
    return chords


def analyse(bm, settings: Settings) -> Analysis:
    """Full flow analysis of an already-repaired, already-de-triangulated mesh."""
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    edge_class = classify_edges(bm, settings)
    polylines = feature_polylines(bm, edge_class)
    vert_class = classify_verts(bm, edge_class, settings)
    chords = trace_chords(bm)

    chord_lens = sorted((len(c) for c in chords), reverse=True)
    a = Analysis(
        edge_class=edge_class,
        vert_class=vert_class,
        polylines=polylines,
        chords=chords,
        stats={
            "edges": len(bm.edges),
            "tris": tri_count(bm),
            "quad_ratio": quad_ratio(bm),
            "locked": sum(1 for c in edge_class.values() if c == LOCKED),
            "feature": sum(1 for c in edge_class.values() if c == FEATURE),
            "free": sum(1 for c in edge_class.values() if c == FREE),
            "polylines": len(polylines),
            "chords": len(chords),
            "chord_mean": (sum(chord_lens) / len(chord_lens)) if chord_lens else 0.0,
            "chord_max": chord_lens[0] if chord_lens else 0,
            "corners": sum(1 for c in vert_class.values() if c == V_CORNER),
        },
    )
    a.stats["structural_floor"] = a.structural_floor()
    return a


def prepare(bm, settings: Settings) -> dict:
    """Stages 0 and 1: repair then quad recovery. Returns merged stats."""
    stats = {"repair": repair(bm, settings)}
    stats["detriangulate"] = detriangulate(bm, settings)
    return stats
