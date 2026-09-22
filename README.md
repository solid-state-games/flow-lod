# FlowLOD

Flow-preserving LOD generation for Blender 4.2+. MIT licensed. No dependencies.

Most Blender LOD addons wrap the Decimate modifier in a percentage field. FlowLOD reads the mesh's
structure first — repairing it, recovering hidden quad topology, and classifying every edge — then
reduces along that structure, and tells you honestly when a budget is impossible.

![LOD ladder](docs/03-lod-ladder.png)

## Install

Copy `flow_lod/` into your Blender addons directory, or zip it and install as an extension.
The panel appears in the 3D viewport sidebar: press **N**, then the **FlowLOD** tab.

## Use

![The panel](docs/01-panel.png)

1. Select a mesh, press **Analyse** to see what you're working with — triangle and vertex counts,
   what welding will save, how much quad flow is recoverable, how many feature edges were found,
   and the structural floor below which budgets become impossible.
2. Add levels with **+**, remove with **−**. Each level is either an absolute triangle target or a
   quality fraction.
3. Press **Bake LODs**. Results land in a `<Name>_LODs` collection; the source is hidden, never
   modified. Re-baking replaces the previous bake rather than accumulating duplicates.

### Tris to Quads

A **switch**, not a separate step. Leave it on and every bake recovers the quad topology a
triangulated export hid before reducing; turn it off to reduce the triangles as they are.

Measured, it helps at moderate budgets and hurts at aggressive ones (F1 88.1% vs 87.8% at 50%,
69.1% vs 71.1% at 25%), which is exactly why it is a switch rather than a fixed stage.

Headless:

```bash
blender -b asset.blend -P flow_lod/cli.py -- --object model --budgets 3000,1500,600 --export out.glb
```

Exits non-zero if any level missed its budget or fell through to unconstrained collapse, so it can
gate a build.

## What it actually does

```
0. REPAIR          weld split vertices back into a manifold mesh
1. TRIS TO QUADS   recover the quad topology a triangulated export hid, flow intact
2. ANALYSE         classify edges LOCKED / FEATURE / FREE, chain features into polylines,
                   trace quad chords, report the structural floor
3. SIMPLIFY        tier 1  whole-chord collapse      (quad rows, flow kept by construction)
                   tier 2  localized chord segments  (Daniels et al. 2009)
                   tier 3  feature-constrained QEM   (Garland & Heckbert, plus constraints)
4. BAKE            re-triangulate, re-mark sharp, emit LOD objects
```

## Measured results

All numbers from `reference-asset.blend`, 6,114 triangles, via `tests/test_flowlod.py`.

**The repair is the biggest single win, and nothing else does it:**

| | verts | edges | open/non-manifold edges |
|---|---|---|---|
| as imported | 13,778 | 16,046 | 13,750 |
| welded | **3,060** | 9,182 | 57 |

78% of the vertices removed at *identical geometry*. Exported meshes arrive with every vertex split
for shading; no simplifier can work on them until this runs, because there are no shared edges and
therefore no edge flow at all.

**Quad recovery works.** De-triangulation at wide angle thresholds recovers 86.9% quads with chords
up to 55 edges long. Quad recovery is a topology problem, not a shape problem — Blender's default
shape heuristics recover only 69.4% and fragment the flow.

**Honest reporting.** The analyser computes a structural floor from the mesh's own hard edges and
flags levels below it *before* baking. On the reference asset that floor is ~1,500 triangles, so a 611
triangle level is flagged as unreachable rather than silently producing mush.

## What it actually is

**A workflow around Blender's Decimate, not a replacement for it.**

The reduction is done by Blender's own Decimate modifier, and FlowLOD's output is verified
*identical* to doing weld-then-Decimate by hand. What the addon adds is everything around that: one
click, per-level budgets, automatic repair, a `<Name>_LODs` collection, consistent naming, a
structural-floor warning, glTF export and a headless CLI.

This addon originally shipped its own pure-Python quadric simplifier. It was measured against
Blender's and lost by 12-17 points at every budget while running 50x slower and missing targets
outright. It is still in the tree behind `Engine: Python QEM` so the comparison stays reproducible,
but nothing should use it.

**Repair is the part that matters most, and it is done automatically.** A reference hull measured
9,509 vertices; welding merged 5,423 of them, 57% of the mesh, with no change to its 8,168
triangles. Meshes arrive from glTF/FBX round trips with vertices split for shading, and a
simplifier cannot collapse across a split. FlowLOD detects this with an exact trial weld and only
welds when there is something to merge.

Structure fidelity against a correctly welded reference:

| | 50% | 25% | 10% |
|---|---|---|---|
| FlowLOD | 90.3% | 76.0% | 47.8% |
| weld then Decimate, by hand | 90.3% | 76.0% | 47.8% |

Identical, which is the point. Every budget is hit exactly, in about 0.1s per level.

### A trap worth knowing

Decimating *without* welding first scores better on a crease metric (80.8% vs 76.0% at 25%). That is
an artifact, not a win: an unwelded mesh has split vertices Decimate cannot collapse across, so it
accidentally preserves creases while carrying more than twice the vertices for the same triangle
count. Vertex count is what costs on the GPU and in a glTF file. Weld first.

### Budgets from measured error, not guesswork

A level can be set three ways: an absolute triangle count, a fraction of the source, or a **maximum
deviation** — the largest shape error you will accept, as a fraction of the model's size.

Deviation is the useful one across a set of assets. FlowLOD measures a real error curve by reducing
at ten ratios and comparing each result to the source, then picks the smallest triangle count that
stays inside your limit. Because a given ratio costs different error on different models, equalising
error rather than ratio is what makes a fleet look consistent. Measured on three hulls:

| asset | dev ≤ 0.003 | dev ≤ 0.008 | dev ≤ 0.015 |
|---|---|---|---|
| A | 90% | 50% | 18% |
| B | 70% | 35% | 18% |
| C | 70% | 25% | 12% |

At the same visual error, A keeps half its triangles where C keeps a quarter. A fixed 25% ladder
would have over-reduced A and under-reduced C. The curve costs well under a second and is only
measured when a level actually asks for it.

This follows the approach in
[Efficient Four-Level LOD Simplification](https://www.mdpi.com/2220-9964/15/2/61) (Sun et al.,
ISPRS IJGI 2026), which makes the same argument: fixed rates lack principled guidance, and uniform
rates suit heterogeneous geometry badly. Their knee-of-the-curve heuristic was measured here and
did not transfer — on four hard-surface hulls the error curve is close to linear in log(triangles)
with no meaningful knee, so the curve is used as a calibration rather than for breakpoint finding.

### Normal-map baking

Reduction throws geometry away; a normal map puts the look of it back. Blender does high-to-low
projection natively, so this needs no extra addon and no dependency — `Bake Normal Map` in the
Output section projects the source onto each LOD.

**Colour textures do not need rebaking.** Decimation preserves the source UV layout almost exactly
(UV area 0.6521 → 0.6472 measured, no degenerate or NaN coordinates, non-overlapping islands), so
every LOD still addresses the original texture set. Only the lost *geometry* is worth capturing.

**The source's own normal map is included.** Blender bakes shading normals, not just geometry, so
an existing normal or bump chain on the source composites into the result. That is why FlowLOD
disconnects the LOD's inherited normal input before wiring the baked map in — otherwise the source
detail would be applied twice, once baked and once live.

The LOD is given **its own copies** of the source materials before any node is touched. Baking must
never edit the asset you baked from, and a test asserts it does not.

`Bake Size` and `Bake Margin` are exposed. Margin bleeds pixels outside each UV island; too low and
island edges show striping. A 1024px bake of an 8k-triangle hull takes well under a second.

### Symmetry

A symmetric model that comes back asymmetric is an obvious defect, and it is the default outcome of
any error-driven simplifier — nothing in a quadric metric knows the left side should match the
right.

**Blender's symmetric decimation is not enough on its own.** `use_symmetry` makes the collapse
*pattern* symmetric, not the geometry. Measured: from a perfectly symmetrized input (worst error
7.5e-13), a symmetric decimation still produced 1.9e-04. Only mirroring the output gives exact
symmetry.

So FlowLOD detects the mirror axis and offers **Symmetrize Output**, in two modes:

| mode | worst mirror error | triangles | UV space |
|---|---|---|---|
| off | 2.7e-02 | 2,040 | separate |
| Mirror | 6.7e-09 | 2,456 | **mirrored** |
| **Rebuild UVs** | **7.2e-08** | 2,400 | **separate** |

**Mirror** replaces one half with a mirror of the other — fast, but it mirrors UVs along with
geometry. If each side of your model has its own UVs (measured on one hull: 1 shared UV cell out of
3,900) then both halves end up sampling the same texture region and asymmetric detail is lost. The
Analyse panel tells you which case you are in.

**Rebuild UVs** avoids that entirely. It cuts down the centre, keeps the *sparser* half, mirrors it
for exact symmetry, gives the mirrored half its own region of the atlas, repacks, and re-bakes both
sides from the source. Symmetry then costs nothing texturally — each side still shows its own
detail, because each side was projected from the corresponding side of the original. It requires
baking, since the UV layout has changed and the old textures no longer apply.

Both modes can overshoot the budget, because mirroring a half may yield more triangles than the
asymmetric result did. The report flags the level as over budget rather than hiding it.

Detection judges on **mean** error, because that is what signals intent: one hull measured 9.8e-05
on X against 1.9e-02 on Y and Z. Its *worst* vertex was 2.1e-02 off, so judging on the worst case
would reject a mesh plainly modelled symmetric. The panel reports that worst figure as drift, so
you can see how far the source has wandered — 2% drift usually means the source itself wants
symmetrizing, not the LOD.

Detection only finds mirroring about the object's **own origin**, which is where Blender enforces
it, so apply transforms first.

### Preprocessing is opt-in, because it is not free

Every preprocessing stage costs some fidelity, so each is a switch and the defaults do the least:

- **Weld** — on, auto-skipped when the mesh is already clean
- **Tris to Quads** — off; recovers hidden quad flow, worth it when you want quads or chord
  collapse, measurably costly on smooth meshes
- **Chord collapse** — auto; on only for quad-modelled sources
- **Cascade** — off; reducing each level from the previous compounds any loss down the ladder
- **Protect strength** — 0; measured not to help, and at aggressive budgets it prevents the target
  being reached at all
- **Symmetry** — auto; detected from the mesh and enforced

## What it does not do, measured

Blender's Decimate modifier beats FlowLOD on raw structure fidelity at matched triangle count:

| | recall | precision | F1 |
|---|---|---|---|
| FlowLOD | **91.5%** | 77.6% | 83.9% |
| Decimate | 80.9% | **93.0%** | **86.5%** |

FlowLOD retains more of the original creases; Decimate produces a cleaner surface with fewer
invented ones.

**The metric and the eye disagree, though.** At an identical 800 triangle budget, chord collapse on
(middle) reads smoother and more coherent than chord collapse off (right), which is visibly lumpy:

![Chords on versus off](docs/04-chords-on-off.png)

The metric scores crease *positions*. It cannot see even topology, sliver triangles, or how a
surface shades in motion. Both numbers and picture are published here because they genuinely point
different ways, and which one matters depends on your asset. Decimate is optimal-position QEM written in C, and it is optimal *by construction* on
exactly this metric — any structure constraint trades geometric error for something else.

That "something else" is loop structure and quad retention, which this metric cannot see. So chord
collapse is **off by default for triangulated imports** and on for genuinely quad-modelled assets
(`Chord Collapse: Auto`). Measured at 3,057 triangles on the reference asset: chords on gives F1 78.8%,
chords off gives 83.9%. Removing whole quad rows is density-uniform, and on a hull whose curvature
varies that deviates more than error-driven collapse does.

Set `Chord Collapse: Always` when even topology and editable loops matter more than geometric
fidelity — a base mesh you will keep working on, rather than a shipped LOD.

Chord collapse is also **17x faster**, because removing whole quad rows is a handful of bulk
operations rather than thousands of individually validated collapses:

| mode | result | quads retained | time |
|---|---|---|---|
| `Never` | 3,056 tris, 1,532 verts | 0% | 3.5s |
| `Always` | 3,055 tris, 1,578 verts | 74% | **0.2s** |

If you are batching hundreds of assets through the CLI, that difference is the whole job.

### Known limits

- Aggressive budgets stall above target. The reference asset reaches 1,169 triangles against a 611 target;
  the link condition blocks further collapses on a mesh with many open edges. Reported, not hidden.
- LODs carry ~1-1.6% non-manifold edges. glTF and Godot are triangle-soup consumers and do not care,
  but it is a defect. Budgeted and asserted in the tests.
- Triangle meshes only for chord work, since chords need quads to exist. Quad-local operations
  (rotations, doublet and singlet removal) are not implemented.
- Chord collapse stops at 25% of the source (`chord_floor`). Pushed past that it removes rows the
  shape depends on and the silhouette collapses — measured, and the reason the guard exists.

## Test

```bash
blender -b ASSET.blend --factory-startup -P tests/test_flowlod.py
```

49 checks against real geometry — repair losslessness, quad recovery, budget adherence, topology
damage budgets, attribute survival, structure fidelity versus the Decimate baseline, and addon
registration. No mocks; the mesh is the fixture.

## Prior art

Built from the papers, not from anyone's source. Garland & Heckbert 1997 (QEM and the
perpendicular-plane boundary constraint); Daniels, Silva, Shepherd & Cohen 2008 and Daniels, Silva &
Cohen 2009 (polychord collapse and its localized form); Tarini et al. 2010 (quad-local operations).
Optiloops, MeshLab and VCGlib are GPL and were deliberately not consulted for implementation.

See `DESIGN.md` for the full reasoning and every measurement.

## License

MIT. See `LICENSE`.
