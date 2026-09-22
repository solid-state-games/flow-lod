"""FlowLOD -- flow-preserving LOD generation for Blender.

Recovers the edge flow a triangulated export hid, reduces along it, and only falls back to error
metrics for what the structure cannot express. See DESIGN.md.
"""

bl_info = {
    "name": "FlowLOD",
    "author": "FlowLOD contributors",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > FlowLOD",
    "description": "Flow-preserving LOD generation with per-level polygon budgets",
    "doc_url": "https://github.com/",
    "category": "Object",
}

import importlib
import sys

import bpy
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from . import analyse, bake

# reload support for development
for _m in (analyse, bake):
    importlib.reload(_m)


# --------------------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------------------

class FlowLODLevel(PropertyGroup):
    mode: EnumProperty(
        name="Budget",
        items=[
            ("TRIS", "Tris", "Absolute triangle target"),
            ("QUALITY", "Quality", "Fraction of the repaired source, 0-1"),
            ("DEVIATION", "Deviation", "Largest acceptable shape error, as a fraction of the "
                                       "model's size. Equalises quality across differently "
                                       "complex assets instead of equalising ratio"),
        ],
        default="TRIS",
    )
    tris: IntProperty(name="Triangles", default=3000, min=4, soft_max=200000)
    quality: FloatProperty(name="Quality", default=0.25, min=0.001, max=1.0)
    deviation: FloatProperty(
        name="Max Deviation", default=0.005, min=0.00001, max=0.5, precision=4,
        description="Largest acceptable mean deviation, as a fraction of the model's bounding "
                    "diagonal. 0.005 is half a percent of the model's size",
    )


class FlowLODSettings(PropertyGroup):
    levels: CollectionProperty(type=FlowLODLevel)
    active: IntProperty(default=0)

    # ponytail: no subtype="ANGLE". That makes Blender store radians while everything downstream
    # works in degrees, which is a units bug waiting to happen. Plain float, labelled.
    feature_angle: FloatProperty(
        name="Feature Angle (deg)", default=70.0, min=0.0, max=180.0,
        description="Dihedral angle above which an edge counts as a feature. "
                    "Bimodal hard-surface meshes need 50 or more; 25 classifies everything",
    )


    clean: BoolProperty(
        name="Clean", default=True,
        description="Remove orphan fragments, dissolve degenerate geometry and make normals "
                    "consistent. Generated meshes routinely arrive with all three",
    )
    remove_hidden: BoolProperty(
        name="Remove Hidden", default=False,
        description="Delete interior geometry no ray can reach from outside. Generated meshes "
                    "carry internal shells worth 9-46%% of their faces. Destructive, and about "
                    "2%% of what it removes is visible at grazing angles, so it runs on the LOD "
                    "copy only and the normal bake recovers what it takes",
    )
    hidden_samples: IntProperty(name="Hidden Samples", default=128, min=8, max=512)

    weld: BoolProperty(
        name="Weld First", default=True,
        description="Merge split vertices, automatically skipped when the mesh has none. "
                    "Exported meshes arrive shattered and nothing can simplify them until "
                    "this runs; clean meshes are left alone",
    )
    detriangulate: BoolProperty(
        name="Tris to Quads", default=False,
        description="Recover the quad topology a triangulated export hid, preserving edge flow, "
                    "before reducing. Measured to help at moderate budgets and hurt at "
                    "aggressive ones, so it is a switch",
    )
    protect_seams: BoolProperty(name="Seams", default=True)
    protect_sharp: BoolProperty(name="Sharp", default=True)
    protect_materials: BoolProperty(name="Material", default=True)
    protect_boundary: BoolProperty(name="Boundary", default=True)
    protect_curvature: BoolProperty(name="Curvature", default=True)

    symmetry: EnumProperty(
        name="Symmetry",
        items=[
            ("AUTO", "Auto", "Detect the mirror plane from the mesh and preserve it"),
            ("X", "X", "Force mirror symmetry across X"),
            ("Y", "Y", "Force mirror symmetry across Y"),
            ("Z", "Z", "Force mirror symmetry across Z"),
            ("NONE", "None", "Do not preserve symmetry"),
        ],
        default="AUTO",
        description="A symmetric model that comes back asymmetric is an obvious defect. "
                    "Detected automatically and enforced during reduction",
    )
    symmetrize: BoolProperty(
        name="Symmetrize Output", default=False,
        description="Mirror each LOD exactly about the detected axis. Blender's symmetric "
                    "decimation alone does NOT guarantee a symmetric result. WARNING: this "
                    "replaces one half with a mirror of the other, including its UVs, so any "
                    "asymmetric texture detail will be mirrored",
    )
    symmetrize_mode: EnumProperty(
        name="Symmetrize Mode",
        items=[
            ("MIRROR", "Mirror", "Replace one half with a mirror of the other. Fast, but mirrors "
                                 "UVs too, so asymmetric texture detail is lost"),
            ("REBUILD", "Rebuild UVs", "Keep the sparser half, mirror it, give the mirrored half "
                                       "its own atlas space and re-bake both sides from the "
                                       "source. Exact symmetry with no texture loss. Requires "
                                       "baking"),
        ],
        default="MIRROR",
        description="How symmetry is enforced",
    )
    cascade: BoolProperty(
        name="Cascade Levels", default=False,
        description="Reduce each level from the previous one rather than from the source, so the "
                    "ladder stays consistent as it descends",
    )
    protect_weight: FloatProperty(
        name="Protect Strength", default=0.0, min=0.0, max=1000.0,
        description="Influence of the protected-vertex group. 0 disables protection entirely",
    )

    selective_protect: BoolProperty(
        name="Selective Protection", default=True,
        description="Protect only junctions where feature lines meet and hard boundaries "
                    "(about 8% of vertices) instead of every feature vertex (about 52%). "
                    "Protecting half the mesh stops any simplifier reaching its budget",
    )

    remark_sharp: BoolProperty(name="Re-mark Sharp", default=False)
    transfer_normals: BoolProperty(
        name="Transfer Normals", default=False,
        description="Copy custom split normals from the source instead of re-deriving them",
    )

    bake_normals: BoolProperty(
        name="Bake Normal Map", default=False,
        description="Project the source geometry onto each LOD as a tangent-space normal map, "
                    "recovering the detail that reduction removed. LODs already inherit the "
                    "source UVs, so the existing colour textures still apply unchanged",
    )
    bake_resolution: IntProperty(name="Bake Size", default=1024, min=64, max=8192)
    bake_margin: IntProperty(
        name="Bake Margin", default=8, min=0, max=64,
        description="Pixels bled outside each UV island. Too low and island edges show striping",
    )

    report: StringProperty(default="")
    cached_stats: StringProperty(default="")


def active_mesh(context):
    """The mesh object to act on, or None.

    context.object raises AttributeError rather than returning None in restricted contexts
    (timers, startup scripts, some operator re-runs), so never touch it directly.
    """
    obj = getattr(context, "object", None)
    if obj is None:
        view_layer = getattr(context, "view_layer", None)
        obj = getattr(view_layer.objects, "active", None) if view_layer else None
    return obj if (obj is not None and obj.type == "MESH") else None


def to_settings(props) -> "analyse.Settings":
    return analyse.Settings(
        clean=props.clean,
        remove_hidden=props.remove_hidden,
        hidden_samples=props.hidden_samples,
        weld=props.weld,
        detriangulate=props.detriangulate,
        feature_angle=props.feature_angle,
        protect_seams=props.protect_seams,
        protect_sharp=props.protect_sharp,
        protect_materials=props.protect_materials,
        protect_boundary=props.protect_boundary,
        protect_curvature=props.protect_curvature,
        selective_protect=props.selective_protect,
        cascade=props.cascade,
        symmetry=props.symmetry,
        symmetrize=props.symmetrize,
        symmetrize_mode=props.symmetrize_mode,
        bake_normals=props.bake_normals,
        bake_resolution=props.bake_resolution,
        bake_margin=props.bake_margin,
        protect_weight=props.protect_weight,
        remark_sharp=props.remark_sharp,
        transfer_normals=props.transfer_normals,
    )


# --------------------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------------------

class FLOWLOD_OT_level_add(Operator):
    bl_idname = "flowlod.level_add"
    bl_label = "Add LOD Level"
    bl_description = "Add an LOD level"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_mesh(context)
        if obj is None:
            self.report({"ERROR"}, "No active mesh object")
            return {"CANCELLED"}
        props = obj.flow_lod
        item = props.levels.add()
        # each new level defaults to half the previous one, which is the usual ladder
        if len(props.levels) > 1:
            prev = props.levels[len(props.levels) - 2]
            item.mode = prev.mode
            item.tris = max(4, prev.tris // 2)
            item.quality = max(0.001, prev.quality * 0.5)
        props.active = len(props.levels) - 1
        return {"FINISHED"}


class FLOWLOD_OT_level_remove(Operator):
    bl_idname = "flowlod.level_remove"
    bl_label = "Remove LOD Level"
    bl_description = "Remove the selected LOD level"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = active_mesh(context)
        return obj is not None and len(obj.flow_lod.levels) > 0

    def execute(self, context):
        obj = active_mesh(context)
        if obj is None:
            self.report({"ERROR"}, "No active mesh object")
            return {"CANCELLED"}
        props = obj.flow_lod
        props.levels.remove(props.active)
        props.active = max(0, props.active - 1)
        return {"FINISHED"}


class FLOWLOD_OT_analyse(Operator):
    bl_idname = "flowlod.analyse"
    bl_label = "Analyse"
    bl_description = "Measure the source mesh without baking anything"

    @classmethod
    def poll(cls, context):
        return active_mesh(context) is not None

    def execute(self, context):
        obj = active_mesh(context)
        if obj is None:
            self.report({"ERROR"}, "No active mesh object")
            return {"CANCELLED"}
        stats = bake.source_stats(obj)
        obj.flow_lod.cached_stats = (
            f"{stats['raw_tris']} tris | {stats['raw_verts']} verts | "
            f"weld -{stats['verts_saved_pct']:.0f}% | quads {stats['quad_ratio']:.0%} | "
            f"features {stats['feature_edges']} | floor ~{stats['structural_floor']} | "
            f"symmetry {stats['symmetry']}"
            + (f" (drift {stats['symmetry_drift']['worst']:.1%} worst)"
               if stats['symmetry'] != "none" else "") +
            ("" if stats.get("uvs_mirrored", True)
             else " | UVs unique per side: Symmetrize would mirror them")
        )
        self.report({"INFO"}, obj.flow_lod.cached_stats)
        return {"FINISHED"}


class FLOWLOD_OT_bake(Operator):
    bl_idname = "flowlod.bake"
    bl_label = "Bake LODs"
    bl_description = "Generate LOD objects into a <Name>_LODs collection"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = active_mesh(context)
        return obj is not None and len(obj.flow_lod.levels) > 0

    def execute(self, context):
        obj = active_mesh(context)
        if obj is None:
            self.report({"ERROR"}, "No active mesh object")
            return {"CANCELLED"}
        props = obj.flow_lod
        levels = [(l.mode, {"TRIS": l.tris, "QUALITY": l.quality,
                            "DEVIATION": l.deviation}[l.mode]) for l in props.levels]

        stats, reports = bake.bake(obj, to_settings(props), levels)
        text = bake.format_report(stats, reports)
        props.report = text
        for line in text.splitlines():
            print("[FlowLOD]", line)

        missed = [r for r in reports if not r["hit_budget"]]
        if missed:
            self.report({"WARNING"},
                        f"{len(missed)} level(s) missed budget -- see the report below the button")
        else:
            self.report({"INFO"}, f"Baked {len(reports)} LOD levels")
        return {"FINISHED"}


# --------------------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------------------

class FLOWLOD_UL_levels(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=f"LOD{index + 1}")
        row.prop(item, "mode", text="")
        field = {"TRIS": "tris", "QUALITY": "quality", "DEVIATION": "deviation"}[item.mode]
        row.prop(item, field, text="")


class FLOWLOD_PT_panel(Panel):
    """Lives in the 3D viewport sidebar (N panel), where mesh work actually happens."""

    bl_label = "FlowLOD"
    bl_idname = "FLOWLOD_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "FlowLOD"

    @classmethod
    def poll(cls, context):
        return active_mesh(context) is not None

    def draw(self, context):
        layout = self.layout
        obj = active_mesh(context)
        if obj is None:
            self.report({"ERROR"}, "No active mesh object")
            return {"CANCELLED"}
        props = obj.flow_lod

        header = layout.row(align=True)
        header.operator("flowlod.analyse", icon="VIEWZOOM")
        if props.cached_stats:
            box = layout.box()
            for chunk in props.cached_stats.split(" | "):
                box.label(text=chunk)

        row = layout.row()
        row.template_list("FLOWLOD_UL_levels", "", props, "levels", props, "active", rows=3)
        col = row.column(align=True)
        col.operator("flowlod.level_add", icon="ADD", text="")
        col.operator("flowlod.level_remove", icon="REMOVE", text="")

        flow = layout.box()
        flow.label(text="Flow")
        flow.prop(props, "feature_angle")
        grid = flow.grid_flow(columns=3, even_columns=True)
        for name in ("protect_seams", "protect_sharp", "protect_materials",
                     "protect_boundary", "protect_curvature"):
            grid.prop(props, name)

        out = layout.box()
        out.label(text="Output")
        out.prop(props, "weld")
        out.prop(props, "clean")
        out.prop(props, "remove_hidden")
        if props.remove_hidden:
            out.prop(props, "hidden_samples")
        out.prop(props, "detriangulate")
        out.prop(props, "selective_protect")
        out.prop(props, "protect_weight")
        out.prop(props, "symmetry")
        out.prop(props, "symmetrize")
        if props.symmetrize:
            out.prop(props, "symmetrize_mode")
            if props.symmetrize_mode == "REBUILD" and not props.bake_normals:
                out.label(text="Rebuild changes UVs: enable Bake Normal Map", icon="ERROR")
        out.prop(props, "bake_normals")
        if props.bake_normals:
            row = out.row(align=True)
            row.prop(props, "bake_resolution")
            row.prop(props, "bake_margin")
        out.prop(props, "cascade")
        out.prop(props, "remark_sharp")
        out.prop(props, "transfer_normals")

        layout.operator("flowlod.bake", icon="MOD_DECIM")

        if props.report:
            box = layout.box()
            for line in props.report.splitlines():
                box.label(text=line)


# --------------------------------------------------------------------------------------

_CLASSES = (
    FlowLODLevel,
    FlowLODSettings,
    FLOWLOD_OT_level_add,
    FLOWLOD_OT_level_remove,
    FLOWLOD_OT_analyse,
    FLOWLOD_OT_bake,
    FLOWLOD_UL_levels,
    FLOWLOD_PT_panel,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.flow_lod = PointerProperty(type=FlowLODSettings)


def unregister():
    del bpy.types.Object.flow_lod
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
