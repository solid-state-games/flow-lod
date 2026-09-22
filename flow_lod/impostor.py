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


def octa_direction(u: float, v: float, full_sphere: bool) -> Vector:
    """Map a cell coordinate in [0,1]^2 to a view direction.

    Full sphere uses the standard octahedral fold, which covers every angle including below.
    Hemisphere uses the hemi-octahedron, which spends the whole atlas on the upper half and so
    resolves side views better. Foliage wants hemisphere; anything seen from underneath, like a
    ship in space, wants the full sphere.
    """
    x = u * 2.0 - 1.0
    y = v * 2.0 - 1.0

    if not full_sphere:
        # hemi-octahedron: rotate the square 45 degrees, z stays positive
        hx = (x + y) * 0.5
        hy = (x - y) * 0.5
        return Vector((hx, hy, 1.0 - abs(hx) - abs(hy))).normalized()

    z = 1.0 - abs(x) - abs(y)
    if z < 0.0:
        x, y = (1.0 - abs(y)) * math.copysign(1.0, x), (1.0 - abs(x)) * math.copysign(1.0, y)
    return Vector((x, y, z)).normalized()


def _capture_material(name: str, mode: str, far: float):
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
        cam = tree.nodes.new("ShaderNodeCameraData")
        div = tree.nodes.new("ShaderNodeMath"); div.operation = "DIVIDE"
        div.inputs[1].default_value = max(1e-6, far)
        tree.links.new(cam.outputs["View Z Depth"], div.inputs[0])
        tree.links.new(div.outputs["Value"], emit.inputs["Color"])

    return mat


def _grid_of_views(obj, grid: int, full_sphere: bool, spacing: float):
    """Instance the object once per atlas cell, each rotated to that cell's view direction."""
    made = []
    target = Vector((0.0, 0.0, 1.0))                 # the camera looks along -Z from above
    for j in range(grid):
        for i in range(grid):
            u = (i + 0.5) / grid
            v = (j + 0.5) / grid
            direction = octa_direction(u, v, full_sphere)
            copy = obj.copy()
            copy.data = obj.data
            bpy.context.scene.collection.objects.link(copy)
            copy.matrix_world = (
                Matrix.Translation(Vector((i * spacing, j * spacing, 0.0)))
                @ direction.rotation_difference(target).to_matrix().to_4x4()
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

    copies = _grid_of_views(obj, grid, full_sphere, spacing)

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
    passes = (
        ("base", "BLENDER_WORKBENCH", None),
        ("norm_depth", eevee, _capture_material("_impostor_normal", "NORMAL", radius * 8.0)),
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
    base = outputs.get("base")
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
