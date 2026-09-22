# Using FlowLOD output in Godot 4 / Redot 4

Covers the LOD meshes and the octahedral impostor. Written against Redot 26.2; Godot 4.x is the
same.

## LOD meshes

Bake with the **Deviation** budget mode if you are doing a fleet. It equalises visual error rather
than triangle ratio, so a simple hull and a complex one look equally good at the same setting,
instead of one being over-reduced.

Export the `<Name>_LODs` collection to glTF, then in Godot set visibility ranges per level:

```gdscript
# LOD1 shows from 0 to 40 m, LOD2 from 40 to 120 m, and so on.
$Ship_LOD1.visibility_range_begin = 0.0
$Ship_LOD1.visibility_range_end = 40.0
$Ship_LOD2.visibility_range_begin = 40.0
$Ship_LOD2.visibility_range_end = 120.0
$Ship_LOD2.visibility_range_begin_margin = 4.0   # fade overlap, avoids popping
```

Godot also generates its own LODs on import. If you are supplying your own, turn that off in the
import dock (**Meshes > Generate LODs**), or you get two systems fighting.

## Impostor

FlowLOD writes three textures next to your blend, or wherever **Atlas Folder** points:

| file | contents |
|---|---|
| `<Name>_impostor_albedo.png` | colour, with the silhouette mask in alpha |
| `<Name>_impostor_normal.png` | per-frame camera-space normals |
| `<Name>_impostor_depth.png` | depth in the red channel, 0.5 at the card plane |

### Shader

`godot/flowlod_impostor.gdshader` in this repo is a Godot 4 port of
[Godot-Octahedral-Impostors](https://github.com/wojtekpil/Godot-Octahedral-Impostors) by wojtekpil
(MIT, see `godot/UPSTREAM-LICENSE`). Upstream targets Godot 3 and will not compile in Godot 4.

The port applies the Godot 4 renames and fixes one real bug: GLSL `sign(0.0)` returns zero, which
collapses both poles onto the same atlas cell, so a camera looking straight down samples the wrong
view. Measured before the fix, a straight-down camera was 90 degrees off.

### Setup

```gdscript
var card := MeshInstance3D.new()
var quad := QuadMesh.new()
quad.size = Vector2(2.0, 2.0)          # match your model's bounding radius
card.mesh = quad

var mat := ShaderMaterial.new()
mat.shader = load("res://flowlod_impostor.gdshader")
mat.set_shader_parameter("imposterTextureAlbedo", load("res://ship_impostor_albedo.png"))
mat.set_shader_parameter("imposterTextureNormal", load("res://ship_impostor_normal.png"))
mat.set_shader_parameter("imposterTextureDepth",  load("res://ship_impostor_depth.png"))
mat.set_shader_parameter("imposterFrames", Vector2(16, 16))   # must match the bake
mat.set_shader_parameter("isFullSphere", true)                # must match the bake
mat.set_shader_parameter("alpha_clamp", 0.3)
mat.set_shader_parameter("depth_scale", 1.0)
card.material_override = mat
```

`imposterFrames` and `isFullSphere` **must** match what you baked. The card stores both as custom
properties (`impostor_frames`, `impostor_full_sphere`), so read them from the imported object rather
than typing them twice.

### Texture import settings

In the import dock, for all three textures:

* **Mipmaps: off.** Mipmapping blends across frame boundaries and bleeds neighbouring views into
  each other.
* **Filter: on** for albedo, **off** for depth if you see banding.
* Normal and depth must be **not** flagged as sRGB. Depth especially: a gamma curve on it warps the
  parallax offset.

### As the last LOD

```gdscript
$Ship_LOD3.visibility_range_end = 300.0
$Ship_impostor.visibility_range_begin = 300.0
$Ship_impostor.visibility_range_end = 0.0        # 0 means no far limit
```

## Known limits

* **No ORM map.** The shader defaults that sampler to white, so occlusion, roughness and metallic
  are flat. Fine for distant objects, which is where impostors are used.
* **The three-frame blend reads slightly fatter than the mesh** up close, because it unions three
  neighbouring silhouettes. Push the impostor further out, or raise `alpha_clamp`, or bake a denser
  grid.
* **Roll at the exact poles.** The upstream shader builds its frame basis two different ways in two
  different functions, with different fallbacks when looking straight up or down. FlowLOD matches
  the vertex-stage one. A camera parked exactly on the pole may show the right shape at the wrong
  in-plane rotation.
