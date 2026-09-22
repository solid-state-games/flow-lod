# FlowLOD — Design Document

**A flow-preserving LOD generator for Blender.**
MIT licensed. Blender 4.2+. No dependencies beyond Blender's bundled Python and numpy.

---

## 1. The problem

Every LOD addon available for Blender today — LODGen, Blender_LOD_Generator, advanced_lod_addon,
EasyLOD — is a wrapper around the Decimate modifier with a percentage field. Decimate Collapse is
topology-blind. It ranks edges by quadric error alone and collapses whatever is cheapest:

- quads become arbitrary triangles
- vertices on a crease get pulled off it, because sliding *along* the surface is cheap
- panel lines smear, hard edges round over, silhouettes lose their read

The information that makes a model look like what it is — its **edge flow**, the deliberate loops the
modeller placed — is exactly what a pure error metric discards first, because a crease is cheap to
slide along.

FlowLOD's thesis: **recover that structure explicitly, reduce along it, and only fall back to error
metrics for what the structure cannot express.**

---

## 2. What the target assets actually are

Measured from the real the source project `.blend` files with Blender 4.5.4 headless. Every number
below was measured, and two of them overturned an earlier version of this design.

| File | Faces | Quad % | Verts | Custom normals |
|---|---|---|---|---|
| Asset A (hero, triangulated) | 6,114 | 0% | 13,778 | yes |
| Asset B (hero, triangulated) | 12,537 | 0% | — | yes |
| Asset C (hero, triangulated) | 9,804 | 0% | — | yes |
| Asset D (hero, triangulated) | 4,571 | 1.7% | — | yes |
| Asset E (hand-modelled kit) | 1,702 | 95% | — | no |

### 2.1 The meshes are shattered — repair is step zero

The reference asset has 13,778 vertices for 6,114 triangles. A closed manifold mesh with 6,114 faces has
roughly 3,057. Of its 16,046 edges, **13,750 are non-manifold or open**: every vertex is split for
shading, the signature of a glTF/FBX round trip.

Welded at 1e-5 of scale: **3,060 verts / 9,182 edges / 57 open edges**, geometrically identical.

This matters twice:

1. **78% of the vertices vanish for free.** Vertex count is what costs in a vertex shader and in a
   glTF file. This is pure profit before any LOD work starts.
2. **It is a hard prerequisite.** Nothing — QEM, chord collapse, Decimate — can operate on a mesh
   whose triangles do not share edges. There is no flow to preserve because there are no shared
   edges. Existing LOD addons produce garbage here because they assume connected input.

LOD0 is the welded source, and it is a real deliverable on its own.

### 2.2 "0% quads" was true and meant nothing

The face census says these meshes are 100% triangles, and an earlier draft of this document concluded
that quad-chord methods were therefore inapplicable and a triangle engine had to be primary.

That was wrong. The wireframe shows an unmistakable quad grid with every quad split by a diagonal:
**this is a quad mesh that was triangulated on export.** The flow is intact, merely hidden.

Measured recovery, after welding, via `bmesh.ops.join_triangles`:

| Recovery settings | Quads | Mean chord | Longest chord | Edges in chords ≥8 |
|---|---|---|---|---|
| 60° thresholds, constrained | 69.4% | 5.7 | 42 | 52% |
| 90° thresholds, constrained | 82.3% | 7.3 | 50 | 64% |
| **180° thresholds, constrained** | **86.9%** | **8.2** | **55** | **70%** |
| 180° thresholds, unconstrained | 90.8% | 9.5 | 97 | 75% |

**Quad recovery is a topology problem, not a shape problem.** Wide-open angle thresholds recover the
most structure; the shape heuristics that `join_triangles` defaults to actively fragment chords by
preferring square-looking pairings over the modeller's original ones.

The chosen default is **180° thresholds with `cmp_seam` / `cmp_sharp` / `cmp_uvs` / `cmp_materials`
enabled**: 86.9% quads, nearly the unconstrained ceiling, without ever merging a pair of triangles
across a seam, a material boundary or a marked-sharp edge. The 4% given up buys correctness.

Since output targets Godot and glTF, which triangulate on import anyway, **quads are a working
representation, not an output format.** We de-triangulate to see the flow, reduce, and re-triangulate
on the way out. It costs nothing.

### 2.3 The custom split normals are decorative

Mean angular deviation between loop normals and their face normal: **1.57°** across 18,342 loops.
2,866 of 6,114 polygons are already flat-shaded.

The custom normal data carries no information beyond "this model is faceted". Re-marking sharp edges
from the feature classification and calling `shade_auto_smooth()` reproduces the shading and removes
a Data Transfer pass plus a category of loop-normal bookkeeping. Real normal transfer stays available
as an option for meshes where this does not hold.

### 2.4 The mesh is hard-surface dense, and this constrains everything

Dihedral histogram **after** de-triangulation (measuring real grid edges, not triangulation
diagonals):

```
  0-10°: 1259    10-30°: 1310    30-60°: 1546    60-80°: 783    80°+: 1384
```

Bimodal. A third of the edges are near-flat, and **1,384 of 6,339 edges exceed 80°**.

Two consequences:

- A 25° feature threshold classifies 62% of edges as features on this mesh — useless. The default
  must be far higher. **`feature_angle` defaults to 50°**, and the UI shows the resulting feature
  count live so a wrong threshold is visible immediately.
- Any single chord of mean length 8 has a high probability of crossing at least one hard edge.
  **Whole-chord collapse stalls quickly.** Measured: a pure whole-chord pass takes the reference asset from
  6,114 to 5,054 tris (17%) and then runs out of legal moves.

That 17% is genuine, perfectly flow-preserving reduction and worth taking first. It is not enough on
its own, which is why the pipeline has three tiers rather than one.

### 2.5 Baseline to beat

Post-weld Decimate Collapse at 50 / 25 / 10%: **3,056 / 1,528 / 610 triangles.**

At the 10% level, 611 triangles cannot retain 1,384 hard edges under any algorithm. The tool must
say so rather than silently producing mush — see §6.4.

---

## 3. Pipeline

```
source mesh
  │
  ├─ 0. REPAIR          weld by distance                    13,778 → 3,060 verts
  ├─ 1. DE-TRIANGULATE  recover quad topology               → 86.9% quads, chords to 55
  ├─ 2. ANALYSE         features + chord graph              on real edges, not diagonals
  ├─ 3. SIMPLIFY        tier 1  whole-chord collapse        flow preserved by construction
  │                     tier 2  localized chord segments    flow preserved at the ends
  │                     tier 3  feature-constrained QEM     flow preserved by constraint
  └─ 4. BAKE            re-triangulate, re-mark sharp, emit LODs
```

The source object is never modified. Everything runs on a bmesh copy.

---

## 4. Stage 0 — Repair

`bmesh.ops.remove_doubles` at `weld_factor × bounding-box diagonal`, default `1e-5`. Scale-relative,
because a 1.9-unit reference asset and a 400-unit station need different epsilons and nobody should have to
think about that. On an already-welded mesh it is a no-op, so it is safe on by default.

**UV guard.** Welding two vertices with different UV coordinates would destroy a UV island boundary.
Before welding, any edge whose two sides disagree on UVs is marked as a seam. The weld then proceeds —
the vertices merge in 3D, which is what we want — and the seam mark both records the discontinuity for
the analyser and causes the exporter to re-split it. UV islands survive.

---

## 5. Stage 1 — Tris to Quads, with the flow preserved

**This stage is a feature in its own right, not just a preprocessing step.** It is exposed as a
standalone operator (`Tris to Quads (Flow)` in the panel, `flowlod.retopo`) because turning a
triangulated import back into an editable quad mesh with its original loops intact is a real task
that has nothing to do with LODs. Run it alone and you get a `<Name>_Quads` object; run a bake and
it happens automatically first.

It always runs **before** LOD generation, because every structured reduction downstream needs quads
to exist. Chords are made of quads; without this stage there is no flow to follow and the pipeline
degrades to plain error-driven collapse.

`bmesh.ops.join_triangles` with both angle thresholds at 180° and all four `cmp_*` comparisons on.

Rationale in §2.2: we want the modeller's original topology back, not attractive quads. Shape
heuristics fragment chords; attribute comparisons prevent merging across real boundaries.

Residual triangles (~13%) are kept as triangles. They are genuine — poles, terminations, hand-built
detail — and are simply not part of any chord. They are handled by tier 3.

If the input is already quad-dominant (>60% quads), this stage is skipped and the mesh proceeds
directly to analysis. The hand-modelled kit at 95% quads takes that path.

---

## 6. Stage 2 — Analyse: recognising the flow

### 6.1 Edge classification

| Class | Condition |
|---|---|
| **LOCKED** | open boundary (`link_faces != 2`), non-manifold (`> 2`), material boundary, UV seam, marked sharp (`edge.smooth == False`), crease > 0, bevel weight > 0 |
| **FEATURE** | manifold and dihedral > `feature_angle` (default **50°**, see §2.4) |
| **FREE** | everything else |

LOCKED is everything the user declared by hand plus everything topologically dangerous. FEATURE is
inferred. Each contributing test is individually toggleable, because a mesh where every edge is a
material boundary should not be frozen solid.

### 6.2 Feature polylines

FEATURE edges chain into **polylines** by walking edge → vertex → edge wherever exactly two feature
edges meet. A polyline is a run of crease that behaves as one curve. This is the object preserved —
not individual edges. A crease may lose vertices along its length; it may not stop being a
continuous, straight, sharp line.

### 6.3 Chord graph

Quad chords are traced by walking quad-to-quad through opposite edges
(`loop.link_loop_next.link_loop_next`). Each chord records, per edge: dihedral angle, classification,
and whether it is a legal dissolve target.

The chord graph and the feature polylines are orthogonal structures over the same mesh. Chords run
*with* the flow; feature polylines typically run *across* it. Their intersections are where
simplification has to be careful, and where tier 2 does its work.

### 6.4 Vertex classification

| Class | Condition |
|---|---|
| **CORNER** | touches a LOCKED edge, **or** junctions ≥3 feature edges, **or** the polyline turns by more than `corner_angle` (default 45°) here |
| **FEATURE** | interior of exactly one feature polyline |
| **FREE** | touches no feature edge |

CORNER vertices are frozen: where lines meet and where lines turn, whose removal is instantly visible.

### 6.5 Honest budget reporting

The analyser counts LOCKED + FEATURE edges and reports the **structural floor**: roughly the triangle
count below which the mesh's own hard edges cannot all survive. On the reference asset that floor is well
above 611 triangles, so a 10% level is flagged in the UI *before* baking, not discovered after.

This is the tool telling the truth about the budget rather than silently producing mush.

---

## 7. Stage 3 — Simplify

### 7.1 Tier 1 — whole-chord collapse

Trace all chords. A chord is a legal target if no edge in it is LOCKED and no edge exceeds
`feature_angle`. Score by mean dihedral across its edges — a chord lying in a flat region costs
nothing to remove. Dissolve cheapest-first via `bmesh.ops.dissolve_edges`, in edge-disjoint batches
for speed, re-tracing between batches.

Quads stay quads. Flow is preserved **by construction**, not by constraint. This is the most
modeller-like reduction available and it is always taken first.

Measured ceiling on the reference asset: 6,114 → 5,054 tris before legal chords run out.

**The chord floor.** Removing whole rows is density-uniform, so pushed far enough it eats rows the
shape depends on and the silhouette collapses. Measured on a subdivided test asset, a 10% budget
reached by chords alone flattened the form completely — the skull sheared off. The structured tiers
therefore stop at `chord_floor` (default 0.25 of the repaired source) and hand the remainder to
error-driven collapse, where preserving the shape matters more than preserving the loops.

*Implementation note:* chord edges must be held as `BMEdge` references, never indices. `dissolve_edges`
invalidates all indices, and an index-based prototype silently collapsed one chord per pass instead of
hundreds.

### 7.2 Tier 2 — localized chord segments

When whole chords run out, the reason is almost always that a long chord is cheap for most of its run
and crosses one hard edge somewhere. Following Daniels, Silva & Cohen 2009, collapse the **cheap
contiguous segment** and stop before the feature.

A segment is a maximal run of consecutive chord edges all below `feature_angle` and none LOCKED, of
at least `min_segment` edges (default 3). Dissolving it leaves the chord's remaining portion intact
and produces a transition at each end, resolved by local retriangulation — the terminating quads
become triangle pairs, which is legal and is exactly where the residual triangle budget goes.

This is where most of the structured reduction happens on dense hard-surface meshes.

### 7.3 Tier 3 — feature-constrained half-edge QEM

For what remains — residual triangles, regions with no usable chords, and aggressive budgets —
Garland & Heckbert quadric error metric with three modifications.

**Constraint quadrics.** For every LOCKED and FEATURE edge, a plane quadric perpendicular to the
adjacent faces and containing the edge is added to both endpoints, scaled by
`feature_weight × length²` (default weight 1000). This is G&H's own boundary-preservation device
applied to inferred features. A vertex on a crease slides *along* it almost free, and pays enormously
to leave it. The crease stays straight and sharp; it just gets fewer vertices.

**Collapse legality**, checked before error matters:

```
FREE     → FREE       allowed
FEATURE  → FEATURE    allowed only if both lie on the SAME polyline, adjacent along it
FEATURE  → FREE       never        ← the single rule that stops flow destruction
CORNER   → anything   never
anything → LOCKED     never
```

`FEATURE → FREE` is the collapse that ruins hard-surface models: cheap by pure quadric error, because
the free vertex usually sits on a flat region, and it drags the crease inward. Forbidding it is most
of the value of this stage.

**Half-edge collapse.** The survivor is one of the two original vertices, the lower-error endpoint,
not a computed optimal position. Because: UVs, colours and material assignments are *inherited* from a
real vertex so an entire class of attribute corruption cannot occur; feature vertices stay exactly on
their line by construction; and it is half the code, with little quality cost once constraints
dominate placement.

**Validity checks** before committing: link condition (the one-rings share exactly the two opposite
vertices — prevents non-manifold creation), no face normal flip beyond 90°, no zero-area result, and
the `max_error` ceiling if set.

**The loop.** Binary heap on collapse error, lazy invalidation via per-vertex version counters, stale
entries discarded on pop rather than removed eagerly, one-ring cost recomputation after each collapse.
At 3k–13k vertices this is well under a second in pure Python.

### 7.4 Relaxation and reporting

If the budget is still unmet after tier 3 with constraints, `feature_angle` rises by 10° and the mesh
is re-analysed, up to 3 times — the weakest feature lines dissolve into FREE first, so flow degrades
gently and in order of importance. Final resort drops constraint quadrics entirely; LOCKED is still
honoured.

Every level records the deepest tier it required. **A level that needed unconstrained QEM is flagged
in the report.** That is not the tool failing; it is the tool saying the budget was unrealistic for
this mesh, which is information worth having before it ships.

### 7.5 What the fidelity measurements actually say

Structure fidelity was measured by classifying every edge of source and LOD, then scoring how well
the LOD's structure edges match the source's — recall (source creases still present) and precision
(LOD creases that existed in the source), combined as F1, at matched triangle count.

| Path | F1 | non-manifold |
|---|---|---|
| chord tiers + QEM | 78.8% | 45 |
| constrained QEM only | 83.1% | 20 |
| unconstrained QEM only | 86.9% | 18 |
| Blender Decimate | **87.5%** | — |

**Every structure-preserving mechanism in this design costs fidelity on that metric.** That is not
a surprise once stated plainly: QEM minimises geometric error by construction, so any constraint
trades error away for something else. Decimate is optimal-position QEM in C and wins outright.

A sweep confirmed the cause is chord collapse rather than the vertex constraints — chords on gives
78.8% regardless of how the corner threshold is set, chords off gives 83–85%. `corner_angle` moved
the result by 0.7 points and is therefore wide by default.

**But the metric and the eye disagree.** Rendered side by side at an identical 800-triangle budget,
the chords-on result reads as smoother and more coherent than chords-off, which is visibly lumpy
(`docs/04-chords-on-off.png`). The metric scores crease *positions*; it cannot see even topology,
sliver triangles, or how a surface shades in motion.

So the defaults split the difference honestly: chord collapse is **off for triangulated imports**
and **on for genuinely quad-modelled assets**, with `Always` and `Never` available. It is also
**17× faster** (0.2s vs 3.5s on the reference asset), which decides it for CLI batch work.

### 7.6 Where this loses, and why

A second reference hull — smooth rather than faceted, 8,168 tris, already a perfect closed manifold
(4,086 verts, zero open, zero non-manifold, so the repair stage is a no-op) — has a completely
different dihedral distribution:

| | ≥30° | ≥50° | ≥70° |
|---|---|---|---|
| faceted hull | ~60% | 42% | 31% |
| smooth hull | 22% | 14% | 6% |

A fixed 70° threshold protects 6% of the smooth hull, so the simplifier runs essentially
unconstrained. That motivated the adaptive threshold in §6.1 — which resolves to 40° here and 75°
on the faceted hull, correctly, **and changed the output almost not at all**: F1 76.6% adaptive vs
77.3% fixed-70 vs 78.0% fixed-30. Within noise.

Structure-fidelity F1 at matched triangle counts on the smooth hull:

| | 50% | 25% |
|---|---|---|
| FlowLOD | 76.6% | 61.6% |
| Blender Decimate | **84.4%** | **75.8%** |

**The dominant factor is almost certainly vertex placement, not feature classification.** §7.3 chose
half-edge collapse so attributes are inherited rather than interpolated — a real benefit that
removes a class of UV bugs. The cost was not appreciated at the time: on a smooth curved surface,
neither endpoint of a collapsed edge sits where the simplified surface should pass, so every
collapse leaves error that optimal-position placement would not.

Other things measured and rejected as fixes:

- **Shape-quality guard** — added, and correct to keep, but not the problem: FlowLOD already
  produced 0.2% slivers against Decimate's 0.8%.
- **Driving Decimate with a feature vertex group** — the obvious "use the better engine" move. It
  fails because the classification marks 52% of vertices as protected, so the modifier stalls at
  4,516 tris against a 4,084 target and fidelity drops to F1 67.6%.

The unresolved work is therefore: implement optimal-position placement with attribute
interpolation, or make the protection far more selective so a vertex-group-driven Decimate can
actually reach a budget. Until one of those lands, this tool's reduction is not competitive on
smooth models and the README says so.

### 7.7 Symmetry

Error-driven simplification has no notion that the left side of a model should match the right, so a
symmetric asset reliably comes back asymmetric — the worst vertex on a measured hull drifted 2.7% of
the bounding diagonal at a 25% budget, which reads immediately as a defect.

Blender's Decimate can enforce mirror symmetry directly, and doing so holds it to machine precision
(1.0e-10). FlowLOD detects the mirror plane rather than asking: `symmetry_error()` measures, per
axis, the mean distance from each vertex to the nearest vertex of the mirrored mesh, and
`detect_symmetry()` picks the axis whose error falls under `symmetry_tolerance` (1e-4 of the
bounding diagonal).

Two limits worth stating. Detection only finds mirroring about the object's **own origin**, because
that is the only plane Blender enforces about — apply transforms first. And symmetry constrains
which edges may collapse, so a level can land slightly under target; the ratio iteration only
corrects overshoot, since under budget is never a problem.

---

## 8. Stage 4 — Bake

- Collection `<Name>_LODs`, linked under the source object's own collection.
- Objects `<Name>_LOD1` … `<Name>_LODn`, transforms matched, source hidden but untouched.
- **Re-triangulate** at the end. Godot and glTF triangulate on import regardless, and the quad
  representation has served its purpose.
- **Sharp re-marking**: edges classed LOCKED or FEATURE in the final mesh get `edge.smooth = False`,
  then `bpy.ops.object.shade_auto_smooth()`. Reproduces the faceted shading without transferring
  loop normals, justified by §2.3. Optional real transfer via Data Transfer modifier.
- Material slots copied wholesale; face material indices survive by inheritance.
- UV layers and colour attributes inherited through half-edge collapse and chord dissolve.

**Report per level**: triangle count, vertex count, deepest tier used, quads retained, feature edges
retained vs source, elapsed time.

---

## 9. Interface

3D viewport sidebar (**N**) → **FlowLOD** tab.

```
┌─ FlowLOD ───────────────────────────────┐
│ Source: 6,114 tris  13,778 verts        │
│ ⚠ 13,750 split edges — weld saves 78%   │
│ ↻ de-triangulates to ~87% quads          │
│                                          │
│ ┌ Levels ───────────────────┐  ┌───┐    │
│ │ LOD1  Tris    3000        │  │ + │    │
│ │ LOD2  Tris    1500        │  │ − │    │
│ │ LOD3  Quality 0.10   ⚠    │  └───┘    │
│ └───────────────────────────┘           │
│                                          │
│ ▾ Flow                                   │
│   Feature Angle      50°   (3930 edges)  │
│   Corner Angle       45°                 │
│   Protect: ☑ Seams ☑ Sharp ☑ Material   │
│            ☑ Boundary ☑ Curvature       │
│ ▾ Output                                 │
│   ☑ Weld first     ☑ De-triangulate      │
│   ☑ Re-mark sharp  ☐ Transfer normals    │
│   Fallback: [Auto-tier ▾]                │
│                                          │
│         [ Bake LODs ]                    │
└──────────────────────────────────────────┘
```

Levels are a `CollectionProperty` on `bpy.types.Object` driven by `UIList` + `template_list`, with
`flowlod.level_add` / `flowlod.level_remove` bound to + and −. Storing settings on the object means
they persist in the `.blend` and survive append and link.

Each level independently selects **Tris** (absolute) or **Quality** (0–1 of the welded source). The ⚠
marks a level below the structural floor of §6.5.

The feature-edge count updates live beside the angle slider — the one piece of feedback that makes a
wrong threshold obvious without a viewport overlay.

---

## 10. Headless use

```bash
blender -b reference-asset.blend -P flow_lod/cli.py -- \
  --object model --budgets 3000,1500,600 --export out/reference asset.glb
```

Same code path as the UI operator, which is a thin shell over the same functions, so CLI and
interactive behaviour cannot drift. `--budgets` takes integers (triangles) or floats ≤ 1 (quality).
Exit code is non-zero if any level fell through to unconstrained QEM, so it can gate a build.

---

## 11. Scope

**In, v1:** weld repair · quad recovery · feature + chord analysis · three-tier simplification ·
per-level tri/quality budgets · structural-floor warnings · UV, colour, material preservation ·
sharp re-marking · single-object UI · headless CLI · Godot/glTF output.

**Out, deliberately:**

| Not building | Why |
|---|---|
| Viewport flow heatmap, live budget scrubbing | The tool should be invisible and correct. The live feature count covers the one case where feedback is essential. |
| Manual protect vertex group | Four automatic protection sources cover it. Add when one is demonstrably missed. |
| Armature weight preservation | No rigged assets in scope. |
| Batch UI, collection sweep | The CLI covers bulk work. |
| Texture atlas, impostors | A different tool. |
| meshoptimizer via compiled wheel | Triangles-only, and a per-platform wheel matrix for meshes where pure Python is sub-second. |
| Full quad-local op set (rotations, doublet/singlet removal) | Tiers 1–3 reach the budgets measured here. Add if a quad-dominant asset stalls. |

---

## 12. Prior art and licensing

**Implemented from:**

- Garland & Heckbert 1997, *Surface Simplification Using Quadric Error Metrics* — QEM core and the
  perpendicular-plane boundary constraint.
- Daniels, Silva, Shepherd & Cohen 2008, *Quadrilateral Mesh Simplification* — polychord collapse.
- Daniels, Silva & Cohen 2009, *Localized Quadrilateral Coarsening* — localized chord segments and
  attribute-weighted selection. The basis of tier 2.
- Tarini, Pietroni, Cignoni, Panozzo & Puppo 2010, *Practical Quad Mesh Simplification* — quad-local
  operations, held in reserve.
- arXiv 2411.16874 (2024), *Single Edge Collapse Quad-Dominant Mesh Reduction* — dihedral-weighted
  quadrics. No code released.

**Studied, not copied:** Optiloops (GPL), MeshLab / VCGlib (GPL). Algorithms are not copyrightable;
implementations are. Everything here is written from the papers.

**Considered and rejected:** meshoptimizer (MIT, excellent) — triangles-only, and requires a compiled
wheel per platform for meshes small enough that pure Python is sub-second.

**License:** MIT, as specified, and GPL-compatible so the repository is unproblematic. If publishing
to extensions.blender.org becomes a goal, the Blender Foundation's position on bpy-linking may require
`SPDX:GPL-2.0-or-later` in the manifest while the source stays MIT. A packaging decision, not a code
one.

---

## 13. Verification

`tests/test_flowlod.py`, run under Blender headless against the real production assets, no mocks:

1. **Repair is lossless** — bounding box and surface area match within tolerance; the reference asset welds
   13,778 → 3,060.
2. **Quad recovery** — the reference asset reaches ≥85% quads with chords of ≥40 edges.
3. **Budgets are hit** — every level lands within 2% of target, or is explicitly flagged as below the
   structural floor.
4. **No topology damage** — no new non-manifold edges beyond the source's 17, no zero-area faces.
5. **Flow is preserved** — ≥90% of source feature polylines have a corresponding polyline in LOD1,
   matched by endpoint proximity. *This is the assertion the project exists for.*
6. **Attributes survive** — UV layer, colour attribute and material slot counts identical at every
   level; no NaN UVs.
7. **It beats the baseline** — sampled point-to-surface distance lower than post-weld Decimate at
   equal triangle count. Baseline: 3,056 / 1,528 / 610.
