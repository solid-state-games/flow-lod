"""Octahedral impostor baking.

A billboard card that samples a grid of pre-rendered views, blending between neighbours as the
camera moves. At distance it is indistinguishable from the mesh and costs two triangles.

Output matches the Godot-Octahedral-Impostors convention (MIT, wojtekpil): an N x N frame atlas,
`base` for albedo and `norm_depth` for normals with depth in alpha, plus shader parameters for
frame count and sphere mode.

The atlas is produced in ONE render rather than N^2 of them: the object is instanced across a grid,
each copy rotated so the camera sees it from that cell's direction. 256 renders becomes 1.
"""

from __future__ import annotations

import math

import bmesh
import bpy
from mathutils import Matrix, Vector


def octa_direction_godot(u: float, v: float, full_sphere: bool) -> Vector:
    """Cell coordinate in [0,1]^2 to a view direction in GODOT space (Y up).

    These are transcriptions of `OctaSphereEnc` and `OctaHemiSphereEnc` from
    Godot-Octahedral-Impostors, so the atlas matches its shader by construction rather than by
    hope. The shader folds on Y and reads the grid from XZ; Blender is Z up, so an earlier version
    of this built the atlas in the wrong frame entirely.
    """
    if full_sphere:
        x = (u - 0.5) * 2.0
        z = (v - 0.5) * 2.0
        y = 1.0 - abs(x) - abs(z)
        if y < 0.0:
            ax, az = abs(x), abs(z)
            x, z = math.copysign(1.0, x) * (1.0 - az), math.copysign(1.0, z) * (1.0 - ax)
        return Vector((x, y, z)).normalized()

    x = u - v
    z = -1.0 + u + v
    y = 1.0 - abs(x) - abs(z)
    return Vector((x, y, z)).normalized()


def frame_basis_godot(direction: Vector):
    """The shader's per-frame basis, transcribed.

        vec3 up = vec3(0,1,0);
        if (abs(z.y) > 0.999) up = vec3(0,0,-1);
        x = normalize(cross(up, z));
        y = normalize(cross(x, z));

    Every cell has to be rendered with this exact in-plane orientation. Using any convenient
    rotation instead, such as the minimal one between two vectors, gives each cell an arbitrary
    roll and the card shows the right shape at the wrong angle.
    """
    z = direction.normalized()
    up = Vector((0.0, 0.0, -1.0)) if abs(z.y) > 0.999 else Vector((0.0, 1.0, 0.0))
    x = up.cross(z).normalized()
    y = x.cross(z).normalized()
    return x, y, z


def godot_to_blender(direction: Vector) -> Vector:
    """Godot (Y up, -Z forward) to Blender (Z up, -Y forward)."""
    return Vector((direction.x, -direction.z, direction.y))


def octa_direction(u: float, v: float, full_sphere: bool) -> Vector:
    """Cell coordinate to a view direction in Blender space."""
    return godot_to_blender(octa_direction_godot(u, v, full_sphere))


def grid_from_direction_godot(direction: Vector, full_sphere: bool):
    """The shader's `VecToSphereOct` / `VecToHemiSphereOct`, for round-trip verification."""
    d = direction.copy()
    if not full_sphere:
        d.y = max(d.y, 0.001)
        d.normalize()
        total = abs(d.x) + abs(d.y) + abs(d.z)
        o = d / total
        return (o.x + o.z, o.z - o.x)

    # GLSL sign(0.0) is 0.0, where Python's copysign(1.0, 0.0) is +1.0. That difference only
    # shows up for exactly axis-aligned directions, and it changes which cell the shader picks.
    def glsl_sign(value):
        return 0.0 if value == 0.0 else math.copysign(1.0, value)

    octant = Vector((glsl_sign(d.x), glsl_sign(d.y), glsl_sign(d.z)))
    total = d.dot(octant)
    o = d / total
    if o.y < 0.0:
        a = Vector((abs(o.x), abs(o.y), abs(o.z)))
        o.x, o.z = octant.x * (1.0 - a.z), octant.z * (1.0 - a.x)
    return (o.x, o.z)


def _capture_material(name: str, mode: str, far):
    """A material that renders normals or depth as colour, for use as a view-layer override."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    out = tree.nodes.new("ShaderNodeOutputMaterial")
    emit = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(emit.outputs["Emission"], out.inputs["Surface"])

    if mode == "NORMAL":
        geo = tree.nodes.new("ShaderNodeNewGeometry")
        # Camera space, so the card's shader can use them directly without a world matrix.
        xf = tree.nodes.new("ShaderNodeVectorTransform")
        xf.vector_type = "NORMAL"
        xf.convert_from = "WORLD"
        xf.convert_to = "CAMERA"
        mul = tree.nodes.new("ShaderNodeVectorMath"); mul.operation = "MULTIPLY_ADD"
        mul.inputs[1].default_value = (0.5, 0.5, 0.5)
        mul.inputs[2].default_value = (0.5, 0.5, 0.5)
        tree.links.new(geo.outputs["Normal"], xf.inputs["Vector"])
        tree.links.new(xf.outputs["Vector"], mul.inputs[0])
        tree.links.new(mul.outputs["Vector"], emit.inputs["Color"])
    else:
        # The shader offsets UVs by (0.5 - depth.r), so the card plane must sit at 0.5 and the
        # surface deviate either side of it. `far` carries the centre distance and radius packed
        # as (centre, radius).
        centre, radius = far
        cam = tree.nodes.new("ShaderNodeCameraData")
        sub = tree.nodes.new("ShaderNodeMath"); sub.operation = "SUBTRACT"
        sub.inputs[1].default_value = centre
        div = tree.nodes.new("ShaderNodeMath"); div.operation = "DIVIDE"
        div.inputs[1].default_value = max(1e-6, radius * 2.0)
        add = tree.nodes.new("ShaderNodeMath"); add.operation = "ADD"
        add.inputs[1].default_value = 0.5
        tree.links.new(cam.outputs["View Z Depth"], sub.inputs[0])
        tree.links.new(sub.outputs["Value"], div.inputs[0])
        tree.links.new(div.outputs["Value"], add.inputs[0])
        tree.links.new(add.outputs["Value"], emit.inputs["Color"])

    return mat


def _grid_of_views(obj, grid: int, full_sphere: bool, spacing: float):
    """Instance the object once per atlas cell, each rotated to that cell's view direction."""
    made = []
    for j in range(grid):
        for i in range(grid):
            u = (i + 0.5) / grid
            v = (j + 0.5) / grid
            gx, gy, gz = frame_basis_godot(octa_direction_godot(u, v, full_sphere))
            bx, by, bz = (godot_to_blender(gx), godot_to_blender(gy), godot_to_blender(gz))
            # Rows, so the matrix sends bx to +X, by to +Y and bz to +Z: the camera looks down -Z
            # with +X right and +Y up, which is the shader's x, y and view direction.
            rotation = Matrix((bx, by, bz)).to_4x4()
            copy = obj.copy()
            copy.data = obj.data
            bpy.context.scene.collection.objects.link(copy)
            copy.matrix_world = (
                Matrix.Translation(Vector((i * spacing, j * spacing, 0.0)))
                @ rotation
                @ Matrix.Translation(-_centre(obj))
            )
            made.append(copy)
    return made


def _apply_override(objects, material):
    """Force a material on these objects without touching the mesh they share."""
    for ob in objects:
        if not ob.material_slots:
            ob.data.materials.append(None)
        for slot in ob.material_slots:
            slot.link = "OBJECT"
            slot.material = material


def _centre(obj) -> Vector:
    local = [Vector(c) for c in obj.bound_box]
    centre = sum(local, Vector()) / 8.0
    return obj.matrix_world @ centre


def _radius(obj) -> float:
    centre = _centre(obj)
    return max((obj.matrix_world @ Vector(c) - centre).length for c in obj.bound_box)


def bake_impostor(obj, settings, directory: str) -> dict:
    """Render the atlas pair and build the billboard card. Returns a report."""
    import os

    grid = max(2, settings.impostor_grid)
    full_sphere = settings.impostor_full_sphere
    resolution = max(64, settings.impostor_resolution)
    radius = _radius(obj) or 1.0
    spacing = radius * 2.2

    scene = bpy.context.scene
    saved = (scene.render.engine, scene.render.resolution_x, scene.render.resolution_y,
             scene.render.filepath, scene.camera, scene.render.film_transparent)

    # The source sits at the world origin, which is exactly where cell (0,0) lands. Left visible
    # it renders into that one cell with its own material, which under EEVEE with no lamps is
    # solid black. Everything else in the scene has to go too.
    hidden_state = [(o, o.hide_render) for o in bpy.data.objects if o.type in {"MESH", "CURVE"}]
    for other, _ in hidden_state:
        other.hide_render = True

    copies = _grid_of_views(obj, grid, full_sphere, spacing)
    for copy in copies:
        copy.hide_render = False

    cam_data = bpy.data.cameras.new("_impostor_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = grid * spacing
    cam = bpy.data.objects.new("_impostor_cam", cam_data)
    scene.collection.objects.link(cam)
    extent = (grid - 1) * spacing * 0.5
    cam.location = Vector((extent, extent, radius * 4.0))
    cam.rotation_euler = (0.0, 0.0, 0.0)
    scene.camera = cam

    scene.render.resolution_x = scene.render.resolution_y = resolution
    scene.render.film_transparent = True
    eevee = "BLENDER_EEVEE_NEXT" if hasattr(scene, "eevee") else "BLENDER_EEVEE"

    os.makedirs(directory, exist_ok=True)
    outputs = {}
    # Albedo comes from Workbench with flat lighting, which renders textures unlit. EEVEE with no
    # lamps renders the object black, which is what a first attempt here produced.
    # Four separate textures, matching the shader's uniforms. An earlier version emitted a single
    # "norm_depth" file on the assumption that depth rode in alpha; the shader actually reads
    # depth from the RED channel of its own texture, and takes the mask from albedo's alpha.
    centre_distance = radius * 4.0
    passes = (
        ("albedo", "BLENDER_WORKBENCH", None),
        ("normal", eevee, _capture_material("_impostor_normal", "NORMAL", None)),
        ("depth", eevee, _capture_material("_impostor_depth", "DEPTH",
                                           (centre_distance, radius))),
    )
    for suffix, engine, override in passes:
        scene.render.engine = engine
        if engine == "BLENDER_WORKBENCH":
            shading = scene.display.shading
            shading.light = "FLAT"
            shading.color_type = "TEXTURE"
            shading.show_object_outline = False
            shading.show_specular_highlight = False
        if override is not None:
            # EEVEE Next ignores view_layer.material_override, so a first attempt here rendered
            # the object's real material unlit, which is black. Object-linked slots override the
            # mesh's materials per object instead, and the copies are throwaway.
            _apply_override(copies, override)
        path = os.path.join(directory, f"{obj.name}_impostor_{suffix}.png")
        scene.render.filepath = path
        try:
            bpy.ops.render.render(write_still=True)
            outputs[suffix] = path
        except Exception as ex:                       # rendering is the fragile part; say why
            outputs[suffix] = f"failed: {ex}"

    for copy in copies:
        bpy.data.objects.remove(copy)
    for other, was_hidden in hidden_state:
        try:
            other.hide_render = was_hidden
        except ReferenceError:
            pass
    bpy.data.objects.remove(cam)
    bpy.data.cameras.remove(cam_data)

    (scene.render.engine, scene.render.resolution_x, scene.render.resolution_y,
     scene.render.filepath, scene.camera, scene.render.film_transparent) = saved

    card = _build_card(obj, radius, grid, full_sphere, outputs)
    return {
        "grid": grid,
        "frames": grid * grid,
        "full_sphere": full_sphere,
        "resolution": resolution,
        "files": outputs,
        "shader_params": {
            "imposterFrames": [grid, grid],
            "isFullSphere": full_sphere,
            "textures": {"albedo": "albedo", "normal": "normal", "depth": "depth"},
        },
        "card": card.name if card else None,
    }


def _build_card(obj, radius: float, grid: int, full_sphere: bool, outputs: dict):
    """A two-triangle quad carrying the atlas and the parameters a shader needs."""
    bm = bmesh.new()
    size = radius
    verts = [bm.verts.new((-size, 0.0, -size)), bm.verts.new((size, 0.0, -size)),
             bm.verts.new((size, 0.0, size)), bm.verts.new((-size, 0.0, size))]
    face = bm.faces.new(verts)
    uv = bm.loops.layers.uv.new("UVMap")
    for loop, coord in zip(face.loops, ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))):
        loop[uv].uv = coord
    me = bpy.data.meshes.new(f"{obj.name}_impostor")
    bm.to_mesh(me)
    bm.free()

    card = bpy.data.objects.new(f"{obj.name}_impostor", me)
    card.location = _centre(obj)
    (obj.users_collection[0] if obj.users_collection
     else bpy.context.scene.collection).objects.link(card)

    mat = bpy.data.materials.new(f"{obj.name}_impostor")
    mat.use_nodes = True
    base = outputs.get("albedo")
    if base and not str(base).startswith("failed"):
        tree = mat.node_tree
        tex = tree.nodes.new("ShaderNodeTexImage")
        try:
            tex.image = bpy.data.images.load(base)
        except Exception:
            pass
        principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if principled is not None and tex.image is not None:
            tree.links.new(tex.outputs["Color"], principled.inputs["Base Color"])
    me.materials.append(mat)

    # What a Godot octahedral impostor shader needs, carried on the object so it survives export.
    card["impostor_frames"] = grid
    card["impostor_full_sphere"] = full_sphere
    return card
