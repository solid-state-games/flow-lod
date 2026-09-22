# FlowLOD

One-click LOD generation for Blender 4.2+. MIT licensed. No dependencies.

![LOD ladder](docs/03-lod-ladder.png)

Set budgets, press bake, get clean LOD meshes in their own collection with normal maps and symmetry
intact. The reduction itself is Blender's Decimate. FlowLOD is everything around it that Blender
otherwise makes you do by hand.

## Install

Copy `flow_lod/` into your Blender addons directory, or zip it and install as an extension.
Press **N** in the 3D viewport and pick the **FlowLOD** tab.

## Use

<img src="docs/01-panel.png" align="right" width="260">

1. Select a mesh and press **Analyse**.
2. Add levels with **+**, remove with **−**.
3. Press **Bake LODs**.

Results land in a `<Name>_LODs` collection. The source is hidden, never modified. Re-baking replaces
the previous bake rather than piling up duplicates.

Headless:

```bash
blender -b asset.blend -P flow_lod/cli.py -- --object model --budgets 3000,1500,600 --export out.glb
```

Exits non-zero if a level missed its budget, so it can gate a build.

<br clear="right">

## Pipeline

| Stage | |
|---|---|
| **Repair** | Welds split vertices. Exported meshes arrive shattered and nothing can simplify them until this runs. |
| **Clean** | Removes orphan fragments, fixes inconsistent normals, drops degenerate faces. |
| **Remove Hidden** | Deletes interior geometry no ray can reach from outside. Opt-in. |
| **Reduce** | Blender's Decimate, driven to your budget. |
| **Symmetry** | Detects the mirror axis and can enforce it exactly. |
| **Bake** | Projects the source onto each LOD as a normal map. |
| **Impostor** | Optional final level: a two-triangle card sampling a grid of pre-rendered views. |

## Budgets

Each level is set one of three ways: an absolute triangle count, a fraction of the source, or a
maximum deviation.

Deviation is the one worth knowing about. FlowLOD measures a real error curve for your mesh, then
picks the triangle count that meets your limit. A given ratio costs different error on different
models, so equalising error rather than ratio is what makes a fleet of assets look consistent
instead of just one of them looking right.

| asset | dev ≤ 0.003 | dev ≤ 0.008 | dev ≤ 0.015 |
|---|---|---|---|
| A | 90% | 50% | 18% |
| B | 70% | 35% | 18% |
| C | 70% | 25% | 12% |

At the same visual error, A keeps half its triangles and C a quarter.

## Symmetry

Error-driven simplification has no idea the left side should match the right, so symmetric models
come back asymmetric. Blender's own symmetric decimation does not fix this. It makes the collapse
pattern symmetric, not the geometry.

**Symmetrize Output** has two modes:

* **Mirror** replaces one half with a mirror of the other. Fast, but it mirrors UVs too, so a model
  whose sides have their own UVs loses that detail.
* **Rebuild UVs** keeps the sparser half, mirrors it, gives the mirror its own atlas space, then
  re-bakes both sides from the source. Exact symmetry at no texture cost. Requires baking.

Analyse tells you which case your UVs are in, and reports how far the source has drifted from
symmetry. Two percent drift usually means the source wants fixing, not the LOD.

## Normal maps

**Bake Normal Map** projects the source geometry onto each LOD. Colour textures need no rebaking:
decimation preserves the UV layout, so LODs still address your original texture set. The source's
own normal map composites into the bake.

## Remove Hidden

Generated meshes carry internal shells nothing can ever see. Measured across four assets, between
0% and 12% of faces after cleaning, and one carried 942 such faces.

FlowLOD casts hemisphere rays from every face and deletes those from which nothing escapes. This is
MeshLab's ambient-occlusion trick applied per face rather than per vertex, because judging by vertex
can delete a visible face that happens to have hidden vertices.

Sampling cannot make it provably safe. Against a ground-truth visibility sweep, 32 rays wrongly flag
5.7% of what they delete; 128 rays plus a minimum connected patch of 8 faces brings that to 2.3%.
Both are the defaults. Isolated flagged faces are treated as sampling noise, which is why one asset
flagged 2 faces and removed none.

The residual risk is bounded by where it runs. FlowLOD never modifies your source, and the normal
map is baked from the original, so a wrongly removed sliver comes back in the bake.

## Octahedral impostors

Past a certain distance no amount of triangle reduction competes with a billboard. **Impostor** adds
a final level: a two-triangle card that samples an N x N atlas of pre-rendered views and blends
between neighbours as the camera moves.

The format follows [Godot-Octahedral-Impostors](https://github.com/wojtekpil/Godot-Octahedral-Impostors)
(MIT) rather than inventing one. Four textures are written, matching that shader's uniforms:
`albedo` (alpha carries the mask), `normal`, `depth` (read from red) and, not yet implemented,
`orm`. Frame count and sphere mode are stored as custom properties on the card.

**Full Sphere** is on by default. Ships are seen from below; foliage is not, and turning it off
spends the whole atlas on the upper hemisphere for better side resolution.

The atlas is produced in a single render. Rather than 256 renders, the mesh is instanced across a
grid with each copy rotated to its cell's view direction. A 16x16 atlas at 2048px takes about two
seconds.

The encoding is a transcription of that shader's own `OctaSphereEnc` and `OctaHemiSphereEnc`, and a
test asserts our encode is the exact inverse of its decode (worst round-trip error 3e-08). Without
that the card samples a different cell than the one rendered, and no amount of shader tuning fixes
it.

### Verified in Redot

Tested against the shader in Redot 26.2 by rendering the impostor and the source mesh from matching
directions. At a 16x16 grid the silhouettes track. Two bugs were found and fixed on the way, neither
visible from the atlas alone:

* **Every cell was rendered at an arbitrary roll.** The shader builds a specific per-frame basis
  (`up = (0,1,0)`, `x = cross(up, z)`, `y = cross(x, z)`); the renderer used whatever rotation was
  convenient. Before the fix no test angle matched, after it most did.
* **The upstream shader collapses both poles onto one cell**, because GLSL `sign(0.0)` is zero. A
  camera looking straight down sampled a view 90 degrees off. `godot/flowlod_impostor.gdshader`
  fixes it.

`docs/GODOT.md` has the setup: shader, parameters, texture import settings and visibility ranges.
All three upstream shader variants ship ported in `godot/`, so there is nothing to port yourself.
The rest of that addon is a Godot-side baker and a distance-swap node, both of which FlowLOD and
Godot's own `visibility_range_*` replace.

### Other gaps

* **No ORM map.** The shader defaults that sampler to white, so occlusion, roughness and metallic
  are flat. Fine at the distance impostors are used.
* **The blend reads fatter than the mesh** close up, because it unions three neighbouring
  silhouettes. Push the impostor further out or bake a denser grid.
* **Roll at the exact poles.** Upstream builds its frame basis two different ways in two functions
  with different pole fallbacks; FlowLOD matches the vertex-stage one.

## Options that cost something

All off by default:

* **Tris to Quads** recovers quad flow hidden by a triangulated export. Helps at moderate budgets,
  hurts at aggressive ones.
* **Cascade** reduces each level from the previous one. Compounds any loss down the ladder.
* **Protect Strength** weights feature vertices against collapse. Measured not to help, and at
  aggressive budgets it stops the budget being reached at all.

## Honest limits

* Decimation inherits whatever the source has. It will not fix non-manifold edges.
* Aggressive budgets degrade hard on detailed hulls. Analyse reports a structural floor so you know
  before baking, not after.
* Symmetrizing can push a level over its triangle budget. The report says so.
* FlowLOD once shipped its own quadric simplifier. It lost to Blender's Decimate by 12 to 17 points
  while running 50 times slower, so it was deleted. `DESIGN.md` keeps the measurements.

## Test

```bash
blender -b ASSET.blend --factory-startup -P tests/test_flowlod.py
```

60 checks against real geometry. No mocks. The mesh is the fixture.

## Origins

Designed and directed by **Solid State Games (SSG)**, built with Claude. The ideas below are SSG's,
and each overturned a conclusion the measurements had apparently already settled.

* **Flow.** The assets profile as 0% quads and the first analysis concluded there was no edge flow to
  preserve; SSG disagreed, and was right, because they are quad meshes that were triangulated on
  export and 86.9% of the quads come back.
* **Clean up.** SSG's remark that the mesh needed merge-by-distance before anything else invalidated
  the whole evaluation, which had been scoring against an unwelded reference and quietly favouring
  methods that skip welding.
* **Half-mirror.** Symmetrizing normally mirrors UVs and destroys per-side texture detail, so SSG
  proposed cutting down the centre, keeping the sparser half, mirroring it and projecting the denser
  side's texture onto the copy, which is now the Rebuild UVs mode and beats a plain mirror on every
  measure.

Also SSG's: delete what did not work, reading GPL projects for ideas is legitimate, the panel belongs
in the N sidebar, Tris to Quads should be a switch rather than a separate step, the assets are
AI-generated rather than kitbashed, the Suzanne demo had lost its chin volume, and the research
direction throughout.

## Prior art

Built from published papers rather than from existing code: Garland & Heckbert 1997 (quadric error
metrics), Daniels et al. 2008 and 2009 (polychord collapse), Sun et al. 2026 (error-curve LOD
budgeting).

Algorithms are not copyrightable, so GPL projects including Optiloops, MeshLab and TexTools were
read where useful. No code was taken from any of them.

`DESIGN.md` has the reasoning and every measurement, including the approaches that failed.

## Licence

MIT. See `LICENSE`.
