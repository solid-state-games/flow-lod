"""Three-tier simplification: whole chords, chord segments, then constrained QEM.

Tier 1 and 2 preserve flow *by construction* -- they remove whole rows of quads, which is what a
modeller does by hand. Tier 3 preserves it *by constraint*, for the residue that has no usable
chord structure and for budgets the structured tiers cannot reach.
"""

from __future__ import annotations

import heapq
import math
from itertools import count

import bmesh
from mathutils import Vector

from .analyse import (
    FREE, FEATURE, LOCKED, V_CORNER, V_FEATURE, V_FREE,
    Settings, analyse, tri_count, quad_ratio,
)

TIER_NONE, TIER_CHORD, TIER_SEGMENT, TIER_QEM, TIER_UNCONSTRAINED = 0, 1, 2, 3, 4
TIER_NAMES = {
    TIER_NONE: "none",
    TIER_CHORD: "whole-chord",
    TIER_SEGMENT: "chord-segment",
    TIER_QEM: "constrained-QEM",
    TIER_UNCONSTRAINED: "unconstrained-QEM",
}


# --------------------------------------------------------------------------------------
# Chord walking
# --------------------------------------------------------------------------------------

def _opposite_in_face(edge, face):
    """The edge across from `edge` within `face` (a quad)."""
    for loop in edge.link_loops:
        if loop.face is face:
            return loop.link_loop_next.link_loop_next.edge
    return None


def walk_ring(seed):
    """The edge ring through `seed`, in traversal order, plus whether it closes into a cycle.

    Order matters: tier 2 needs contiguous runs, which an unordered flood fill cannot express.

    Collapsing an edge ring is polychord collapse -- it removes one row of quads and leaves quads
    behind. Verified on a clean 8x8 grid: 64 quads -> 56 quads, zero ngons.  Dissolving the ring
    instead merges the whole strip into one ngon, which is the wrong operation entirely.
    """
    quads = [f for f in seed.link_faces if len(f.verts) == 4]
    seen = {seed}
    faces_seen = set()
    sides = ([], [])
    cycle = False
    self_intersecting = False

    for i, f0 in enumerate(quads[:2]):
        acc = sides[i]
        edge, face = seed, f0
        while True:
            if face in faces_seen:
                self_intersecting = True
                break
            faces_seen.add(face)
            nxt = _opposite_in_face(edge, face)
            if nxt is None:
                break
            if nxt in seen:
                cycle = True
                break
            seen.add(nxt)
            acc.append(nxt)
            nf = next((f for f in nxt.link_faces
                       if f is not face and len(f.verts) == 4), None)
            if nf is None:
                break
            edge, face = nxt, nf

    return list(reversed(sides[1])) + [seed] + sides[0], (cycle, self_intersecting)


def all_rings(bm):
    """Every edge ring, ordered, each edge appearing in exactly one."""
    seen = set()
    out = []
    for e in bm.edges:
        if e in seen or not any(len(f.verts) == 4 for f in e.link_faces):
            continue
        ring, (cycle, self_int) = walk_ring(e)
        seen.update(ring)
        if self_int:
            # A ring that revisits a face crosses itself. Collapsing it merges geometry onto
            # itself and is the main source of non-manifold damage in polychord methods.
            continue
        out.append((ring, cycle))
    return out


# --------------------------------------------------------------------------------------
# Tier 1 + 2: chord collapse
# --------------------------------------------------------------------------------------

def _edge_angle(e):
    return e.calc_face_angle(0.0) if len(e.link_faces) == 2 else math.pi


def _runs(ring, edge_class, feat_rad, min_len):
    """Maximal contiguous runs of collapsible edges within an ordered ring."""
    runs, cur = [], []
    for e in ring:
        ok = edge_class.get(e) != LOCKED and _edge_angle(e) <= feat_rad
        if ok:
            cur.append(e)
        else:
            if len(cur) >= min_len:
                runs.append(cur)
            cur = []
    if len(cur) >= min_len:
        runs.append(cur)
    return runs


def _cost(edges):
    angles = [_edge_angle(e) for e in edges]
    return sum(angles) / len(angles) if angles else math.pi


def collapse_chords(bm, settings: Settings, target: int, whole_only: bool) -> dict:
    """Collapse quad rows until the budget is met or no legal row remains.

    whole_only=True is tier 1: a ring must be entirely collapsible, which is the cleanest possible
    reduction but stalls fast on dense hard-surface meshes -- a ring of mean length 8 will almost
    always cross one hard edge somewhere.

    whole_only=False is tier 2 (Daniels, Silva & Cohen 2009): collapse the cheap contiguous run and
    stop before the feature. This is where most structured reduction happens.
    """
    feat_rad = math.radians(settings.feature_angle)
    collapsed = 0
    rounds = 0

    while tri_count(bm) > target and rounds < 200:
        rounds += 1
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        edge_class = _cheap_classify(bm, settings)

        candidates = []
        for ring, _cycle in all_rings(bm):
            if whole_only:
                if len(ring) < 2:
                    continue
                if any(edge_class.get(e) == LOCKED or _edge_angle(e) > feat_rad for e in ring):
                    continue
                candidates.append((_cost(ring), ring))
            else:
                for run in _runs(ring, edge_class, feat_rad, settings.min_segment):
                    candidates.append((_cost(run), run))

        if not candidates:
            break

        candidates.sort(key=lambda c: c[0])

        # Collapse a batch of the cheapest disjoint runs, then re-trace. Batching keeps the number
        # of full re-traces small; disjointness keeps the batch topologically independent.
        batch, used, projected = [], set(), tri_count(bm)
        for _c, edges in candidates:
            if projected <= target:
                break
            if any(e in used for e in edges):
                continue
            used.update(edges)
            batch.append(edges)
            projected -= len(edges) * 2

        if not batch:
            break

        flat = [e for run in batch for e in run if e.is_valid]
        if not flat:
            break
        try:
            bmesh.ops.collapse(bm, edges=flat, uvs=True)
        except Exception:
            break
        collapsed += len(batch)

        # collapse leaves degenerate/duplicate geometry behind; clean before re-tracing
        bmesh.ops.dissolve_degenerate(bm, dist=1e-7, edges=bm.edges[:])

    return {"rows_collapsed": collapsed, "rounds": rounds, "tris": tri_count(bm)}


def _cheap_classify(bm, settings: Settings) -> dict:
    """Edge locking only -- the part that must be re-evaluated after every topology change.

    ponytail: full analyse() also builds polylines, vertex classes and the chord graph, none of
    which the chord tiers read. Recomputing all of it per round was 4x the cost for nothing.
    """
    uv_layer = bm.loops.layers.uv.active
    crease = bm.edges.layers.float.get("crease_edge")
    bevel = bm.edges.layers.float.get("bevel_weight_edge")

    from .analyse import _uv_discontinuous
    out = {}
    for e in bm.edges:
        if len(e.link_faces) != 2:
            out[e] = LOCKED
        elif settings.protect_sharp and not e.smooth:
            out[e] = LOCKED
        elif settings.protect_seams and (e.seam or _uv_discontinuous(e, uv_layer)):
            out[e] = LOCKED
        elif settings.protect_materials and \
                e.link_faces[0].material_index != e.link_faces[1].material_index:
            out[e] = LOCKED
        elif crease is not None and e[crease] > 0.0:
            out[e] = LOCKED
        elif bevel is not None and e[bevel] > 0.0:
            out[e] = LOCKED
        else:
            out[e] = FREE
    return out


# --------------------------------------------------------------------------------------
# Tier 3: feature-constrained half-edge QEM
# --------------------------------------------------------------------------------------

def _plane_quadric(n: Vector, d: float):
    """Symmetric 4x4 as 10 floats: a2 ab ac ad b2 bc bd c2 cd d2."""
    a, b, c = n
    return (a * a, a * b, a * c, a * d,
            b * b, b * c, b * d,
            c * c, c * d,
            d * d)


def _q_add(p, q):
    return tuple(x + y for x, y in zip(p, q))


def _q_scale(p, s):
    return tuple(x * s for x in p)


_ZERO_Q = (0.0,) * 10


def _q_error(q, v: Vector) -> float:
    x, y, z = v
    (a2, ab, ac, ad, b2, bc, bd, c2, cd, d2) = q
    return (a2 * x * x + 2 * ab * x * y + 2 * ac * x * z + 2 * ad * x
            + b2 * y * y + 2 * bc * y * z + 2 * bd * y
            + c2 * z * z + 2 * cd * z
            + d2)


def _build_quadrics(bm, edge_class, settings: Settings, constrained: bool):
    q = {v: _ZERO_Q for v in bm.verts}

    for f in bm.faces:
        n = f.normal
        if n.length_squared < 1e-16:
            continue
        n = n.normalized()
        d = -n.dot(f.verts[0].co)
        fq = _plane_quadric(n, d)
        for v in f.verts:
            q[v] = _q_add(q[v], fq)

    if not constrained:
        return q

    # Garland & Heckbert's boundary-constraint device, applied to inferred features as well as
    # real borders: a plane perpendicular to the adjacent faces and containing the edge. A vertex
    # on a crease slides along it almost free and pays enormously to leave it.
    w = 1000.0
    for e, cls in edge_class.items():
        if cls == FREE or not e.is_valid:
            continue
        v0, v1 = e.verts
        direction = (v1.co - v0.co)
        length = direction.length
        if length < 1e-12:
            continue
        direction = direction / length

        for f in e.link_faces:
            fn = f.normal
            if fn.length_squared < 1e-16:
                continue
            n = direction.cross(fn.normalized())
            if n.length_squared < 1e-16:
                continue
            n = n.normalized()
            cq = _q_scale(_plane_quadric(n, -n.dot(v0.co)), w * length * length)
            q[v0] = _q_add(q[v0], cq)
            q[v1] = _q_add(q[v1], cq)

    return q


def _legal_collapse(v0, v1, vert_class, edge_class, e, constrained: bool) -> bool:
    """Can v0 be merged into v1 without destroying structure or topology?"""
    if edge_class.get(e) == LOCKED:
        return False

    if constrained:
        c0, c1 = vert_class.get(v0, V_FREE), vert_class.get(v1, V_FREE)
        if c0 == V_CORNER:
            return False
        # FEATURE -> FREE is the collapse that ruins hard-surface models: cheap by pure quadric
        # error because the free vertex sits on a flat region, and it drags the crease inward.
        if c0 == V_FEATURE and c1 == V_FREE:
            return False
        if c0 == V_FEATURE and c1 == V_FEATURE and edge_class.get(e) != FEATURE:
            return False

    # link condition: one-rings must share exactly the vertices opposite this edge
    n0 = {x.other_vert(v0) for x in v0.link_edges}
    n1 = {x.other_vert(v1) for x in v1.link_edges}
    opposite = set()
    for f in e.link_faces:
        for v in f.verts:
            if v is not v0 and v is not v1:
                opposite.add(v)
    return (n0 & n1) == opposite


def _flips_normal(v0, v1) -> bool:
    """Would moving v0 onto v1 invert or degenerate any face that survives?"""
    for f in v0.link_faces:
        if v1 in f.verts:
            continue                       # this face disappears in the collapse
        before = f.normal.copy()
        if before.length_squared < 1e-16:
            continue
        pts = [(v1.co if v is v0 else v.co) for v in f.verts]
        after = (pts[1] - pts[0]).cross(pts[2] - pts[0])
        if after.length_squared < 1e-20:
            return True
        if before.normalized().dot(after.normalized()) < 0.0:
            return True
    return False


def qem_simplify(bm, settings: Settings, target: int, constrained: bool = True) -> dict:
    """Half-edge collapse under quadric error, with structure constraints.

    Half-edge (survivor is one of the two originals) rather than optimal-position: UVs, colours
    and material assignments are inherited from a real vertex so attribute corruption cannot
    occur, and feature vertices stay exactly on their line by construction.
    """
    bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 3])
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.normal_update()

    an = analyse(bm, settings)
    edge_class, vert_class = an.edge_class, an.vert_class
    quad = _build_quadrics(bm, edge_class, settings, constrained)

    version = {v: 0 for v in bm.verts}
    tick = count()
    heap = []

    def push(e):
        if not e.is_valid:
            return
        v0, v1 = e.verts
        for a, b in ((v0, v1), (v1, v0)):
            if not _legal_collapse(a, b, vert_class, edge_class, e, constrained):
                continue
            err = _q_error(_q_add(quad[a], quad[b]), b.co)
            heapq.heappush(heap, (err, next(tick), a, b, version[a], version[b]))

    for e in bm.edges:
        push(e)

    collapsed = 0
    while heap and tri_count(bm) > target:
        err, _t, v0, v1, ver0, ver1 = heapq.heappop(heap)
        if not (v0.is_valid and v1.is_valid):
            continue
        if version.get(v0) != ver0 or version.get(v1) != ver1:
            continue                                   # stale entry, lazy invalidation

        e = next((x for x in v0.link_edges if x.other_vert(v0) is v1), None)
        if e is None or not e.is_valid:
            continue
        if not _legal_collapse(v0, v1, vert_class, edge_class, e, constrained):
            continue
        if _flips_normal(v0, v1):
            continue

        merged = _q_add(quad[v0], quad[v1])
        neighbours = [x.other_vert(v0) for x in v0.link_edges]

        try:
            bmesh.ops.pointmerge(bm, verts=[v0, v1], merge_co=v1.co)
        except Exception:
            continue

        collapsed += 1
        # pointmerge does not promise which BMVert survives, and the periodic degenerate cleanup
        # can remove it outright. Re-check before touching it.
        if not v1.is_valid:
            continue
        quad[v1] = merged
        version[v1] = version.get(v1, 0) + 1
        for nb in neighbours:
            if nb.is_valid:
                version[nb] = version.get(nb, 0) + 1

        if collapsed % 64 == 0:
            bmesh.ops.dissolve_degenerate(bm, dist=1e-7, edges=bm.edges[:])
            if not v1.is_valid:
                continue

        for x in v1.link_edges:
            push(x)

    bmesh.ops.dissolve_degenerate(bm, dist=1e-7, edges=bm.edges[:])
    return {"collapsed": collapsed, "tris": tri_count(bm)}


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------

def simplify_to(bm, settings: Settings, target: int,
                source_was_quad_dominant: bool = False) -> dict:
    """Run the tiers in order until the budget is met. Reports the deepest tier it needed.

    source_was_quad_dominant drives the AUTO chord decision: a hand-modelled quad asset keeps its
    loops, a triangulated import gets the higher-fidelity error-driven path. See Settings.use_chords.
    """
    report = {"target": target, "start_tris": tri_count(bm), "tiers": []}
    deepest = TIER_NONE

    chords_on = settings.use_chords == "ALWAYS" or (
        settings.use_chords == "AUTO" and source_was_quad_dominant
    )
    report["chords_used"] = chords_on

    # Never let the structured tiers drive the mesh below the chord floor; past that point
    # removing whole rows destroys the silhouette faster than it saves triangles.
    chord_target = max(target, int(report["start_tris"] * settings.chord_floor))
    report["chord_target"] = chord_target if chords_on else None

    if chords_on and tri_count(bm) > chord_target:
        r = collapse_chords(bm, settings, chord_target, whole_only=True)
        if r["rows_collapsed"]:
            deepest = TIER_CHORD
            report["tiers"].append(("whole-chord", r))

    if chords_on and tri_count(bm) > chord_target:
        r = collapse_chords(bm, settings, chord_target, whole_only=False)
        if r["rows_collapsed"]:
            deepest = TIER_SEGMENT
            report["tiers"].append(("chord-segment", r))

    if tri_count(bm) > target:
        r = qem_simplify(bm, settings, target, constrained=True)
        if r["collapsed"]:
            deepest = TIER_QEM
            report["tiers"].append(("constrained-QEM", r))

    # Relaxation: weakest feature lines dissolve first, so flow degrades in order of importance.
    relaxed = settings
    for _ in range(3):
        if tri_count(bm) <= target:
            break
        relaxed = Settings(**{**relaxed.__dict__, "feature_angle": relaxed.feature_angle + 10.0})
        r = qem_simplify(bm, relaxed, target, constrained=True)
        if r["collapsed"]:
            report["tiers"].append((f"relaxed-{relaxed.feature_angle:.0f}deg", r))

    if tri_count(bm) > target:
        r = qem_simplify(bm, settings, target, constrained=False)
        if r["collapsed"]:
            deepest = TIER_UNCONSTRAINED
            report["tiers"].append(("unconstrained-QEM", r))

    report["deepest_tier"] = deepest
    report["deepest_tier_name"] = TIER_NAMES[deepest]
    report["final_tris"] = tri_count(bm)
    report["final_verts"] = len(bm.verts)
    report["quad_ratio"] = quad_ratio(bm)
    report["hit_budget"] = tri_count(bm) <= target
    return report
