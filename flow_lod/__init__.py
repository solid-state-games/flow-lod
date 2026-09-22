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

from . import analyse, bake, simplify

# reload support for development
for _m in (analyse, simplify, bake):
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
        ],
        default="TRIS",
    )
    tris: IntProperty(name="Triangles", default=3000, min=4, soft_max=200000)
    quality: FloatProperty(name="Quality", default=0.25, min=0.001, max=1.0)


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
    corner_angle: FloatProperty(name="Corner Angle (deg)", default=180.0, min=0.0, max=180.0)

    use_chords: EnumProperty(
        name="Chord Collapse",
        items=[
            ("AUTO", "Auto", "On for quad-modelled sources, off for triangulated imports"),
            ("ALWAYS", "Always", "Keep quad loops even where error-driven collapse is more accurate"),
            ("NEVER", "Never", "Pure error-driven collapse, highest geometric fidelity"),
        ],
        default="AUTO",
        description="Removing whole quad rows keeps loop structure but is density-uniform, which "
                    "costs geometric fidelity on hulls with varying curvature",
    )

    weld: BoolProperty(
        name="Weld First", default=True,
        description="Merge split vertices. Exported meshes arrive shattered and nothing "
                    "can simplify them until this runs",
    )
    detriangulate: BoolProperty(
        name="Tris to Quads", default=True,
        description="Recover the quad topology a triangulated export hid, preserving edge flow, "
                    "before reducing. Measured to help at moderate budgets and hurt at "
                    "aggressive ones, so it is a switch",
    )
    protect_seams: BoolProperty(name="Seams", default=True)
    protect_sharp: BoolProperty(name="Sharp", default=True)
    protect_materials: BoolProperty(name="Material", default=True)
    protect_boundary: BoolProperty(name="Boundary", default=True)
    protect_curvature: BoolProperty(name="Curvature", default=True)

    selective_protect: BoolProperty(
        name="Selective Protection", default=True,
        description="Protect only junctions where feature lines meet and hard boundaries "
                    "(about 8% of vertices) instead of every feature vertex (about 52%). "
                    "Protecting half the mesh stops any simplifier reaching its budget",
    )

    remark_sharp: BoolProperty(name="Re-mark Sharp", default=True)
    transfer_normals: BoolProperty(
        name="Transfer Normals", default=False,
        description="Copy custom split normals from the source instead of re-deriving them",
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
        weld=props.weld,
        detriangulate=props.detriangulate,
        feature_angle=props.feature_angle,
        corner_angle=props.corner_angle,
        use_chords=props.use_chords,
        protect_seams=props.protect_seams,
        protect_sharp=props.protect_sharp,
        protect_materials=props.protect_materials,
        protect_boundary=props.protect_boundary,
        protect_curvature=props.protect_curvature,
        selective_protect=props.selective_protect,
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
            f"features {stats['feature_edges']} | floor ~{stats['structural_floor']}"
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
        levels = [(l.mode, l.tris if l.mode == "TRIS" else l.quality) for l in props.levels]

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
        row.prop(item, "tris" if item.mode == "TRIS" else "quality", text="")


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
        flow.prop(props, "corner_angle")
        flow.prop(props, "use_chords")
        grid = flow.grid_flow(columns=3, even_columns=True)
        for name in ("protect_seams", "protect_sharp", "protect_materials",
                     "protect_boundary", "protect_curvature"):
            grid.prop(props, name)

        out = layout.box()
        out.label(text="Output")
        out.prop(props, "weld")
        out.prop(props, "detriangulate")
        out.prop(props, "selective_protect")
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
