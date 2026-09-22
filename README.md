# FlowLOD

One-click LOD generation for Blender 4.2+. MIT licensed. No dependencies.

![LOD ladder](docs/03-lod-ladder.png)

Set budgets, press bake, get clean LOD meshes in their own collection with normal maps and symmetry
intact. The reduction is Blender's own Decimate — FlowLOD is everything around it that Blender
makes you do by hand.

## Install

Copy `flow_lod/` into your Blender addons directory, or zip it and install as an extension.
Press **N** in the 3D viewport and pick the **FlowLOD** tab.

## Use

![The panel](docs/01-panel.png)

1. Select a mesh and press **Analyse** to see what you're working with.
2. Add levels with **+**, remove with **−**.
3. Press **Bake LODs**.

Results land in a `<Name>_LODs` collection. The source is hidden, never modified. Re-baking
replaces the previous bake.

Headless:

```bash
blender -b asset.blend -P flow_lod/cli.py -- --object model --budgets 3000,1500,600 --export out.glb
```

Exits non-zero if a level missed its budget, so it can gate a build.

## What it does

| Stage | |
|---|---|
| **Repair** | Welds split vertices. Auto-skipped when there is nothing to merge. |
| **Clean** | Removes orphan fragments, fixes inconsistent normals, drops degenerate faces. |
| **Reduce** | Blender's Decimate, driven to your budget. |
| **Symmetry** | Detects the mirror axis and can enforce it exactly. |
| **Bake** | Projects the source onto each LOD as a normal map. |

## Budgets

Each level is set one of three ways:

- **Tris** — an absolute triangle count.
- **Quality** — a fraction of the source.
- **Deviation** — the largest shape error you accept, as a fraction of the model's size.

Deviation is the one worth knowing about. FlowLOD measures a real error curve for your mesh and
picks the triangle count that meets your limit. Because the same ratio costs different error on
different models, this is what makes a *fleet* of assets look consistent rather than a single one
look right:

| asset | dev ≤ 0.003 | dev ≤ 0.008 | dev ≤ 0.015 |
|---|---|---|---|
| A | 90% | 50% | 18% |
| B | 70% | 35% | 18% |
| C | 70% | 25% | 12% |

At the same visual error, A keeps half its triangles and C a quarter.

## Symmetry

Error-driven simplification has no idea the left side should match the right, so symmetric models
come back asymmetric. Blender's own symmetric decimation does **not** fix this — it makes the
collapse pattern symmetric, not the geometry.

**Symmetrize Output** has two modes:

- **Mirror** — replaces one half with a mirror of the other. Fast, but mirrors UVs too, so a model
  whose sides have unique UVs loses that detail.
- **Rebuild UVs** — keeps the sparser half, mirrors it, gives the mirror its own atlas space, and
  re-bakes both sides from the source. Exact symmetry at no texture cost. Requires baking.

The Analyse panel tells you which case your UVs are in, and reports how far the source has drifted
from symmetry — 2% drift usually means the source wants fixing, not the LOD.

## Normal maps

**Bake Normal Map** projects the source geometry onto each LOD. Colour textures need no rebaking:
decimation preserves the UV layout, so LODs still address your original texture set. The source's
own normal map composites into the bake.

## Options worth knowing

Everything below is off by default because it costs something:

- **Tris to Quads** — recovers quad flow hidden by a triangulated export. Helps at moderate
  budgets, hurts at aggressive ones.
- **Cascade** — reduce each level from the previous. Compounds any loss down the ladder.
- **Protect Strength** — weights feature vertices against collapse. Measured not to help, and at
  aggressive budgets it prevents the budget being reached.

## Honest limits

- Decimation inherits whatever the source has. It will not fix non-manifold edges.
- Aggressive budgets degrade hard on detailed hulls. The Analyse panel reports a structural floor
  so you know before baking.
- Symmetrizing can push a level over its triangle budget; the report says so.
- FlowLOD shipped its own quadric simplifier and it lost to Blender's Decimate by 12–17 points
  while running 50× slower. It was deleted. `DESIGN.md` records the measurements.

## Test

```bash
blender -b ASSET.blend --factory-startup -P tests/test_flowlod.py
```

52 checks against real geometry. No mocks — the mesh is the fixture.

## Prior art

Built from published papers rather than existing code: Garland & Heckbert 1997 (quadric error
metrics), Daniels et al. 2008/2009 (polychord collapse), Sun et al. 2026 (error-curve LOD
budgeting). Algorithms are not copyrightable, so GPL projects like Optiloops, MeshLab and TexTools
were read for understanding where useful — no code was taken from any of them.

See `DESIGN.md` for the reasoning and every measurement, including the approaches that failed.

## License

MIT. See `LICENSE`.
