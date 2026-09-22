"""Headless entry point.

    blender -b asset.blend -P flow_lod/cli.py -- --object model --budgets 3000,1500,600

Exits non-zero if any level fell through to unconstrained QEM, so it can gate a build.
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy


def _args(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="flowlod")
    p.add_argument("--object", default=None, help="object name; default is the largest mesh")
    p.add_argument("--budgets", required=True,
                   help="comma separated; ints are triangles, floats <=1 are quality")
    p.add_argument("--feature-angle", type=float, default=50.0)
    p.add_argument("--no-weld", action="store_true")
    p.add_argument("--no-detriangulate", action="store_true")
    p.add_argument("--export", default=None, help="write a .glb here")
    p.add_argument("--save", default=None, help="save the .blend here")
    return p.parse_args(argv)


def _pick_object(name):
    if name:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            sys.exit(f"flowlod: no mesh object named {name!r}")
        return obj
    meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.data.polygons]
    if not meshes:
        sys.exit("flowlod: no mesh objects in this file")
    return max(meshes, key=lambda o: len(o.data.polygons))


def main():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from flow_lod.analyse import Settings
    from flow_lod.bake import bake, format_report

    a = _args(sys.argv)
    levels = []
    for tok in a.budgets.split(","):
        tok = tok.strip()
        value = float(tok)
        levels.append(("QUALITY", value) if value <= 1.0 else ("TRIS", int(value)))

    obj = _pick_object(a.object)
    settings = Settings(
        weld=not a.no_weld,
        detriangulate=not a.no_detriangulate,
        feature_angle=a.feature_angle,
    )

    stats, reports = bake(obj, settings, levels)
    print(format_report(stats, reports))

    if a.export:
        os.makedirs(os.path.dirname(os.path.abspath(a.export)) or ".", exist_ok=True)
        bpy.ops.object.select_all(action="DESELECT")
        coll = bpy.data.collections.get(f"{obj.name}_LODs")
        for o in coll.objects:
            o.select_set(True)
        bpy.ops.export_scene.gltf(filepath=a.export, use_selection=True)
        print(f"exported {a.export}")

    if a.save:
        bpy.ops.wm.save_as_mainfile(filepath=a.save)
        print(f"saved {a.save}")

    missed = [r for r in reports if not r["hit_budget"]]
    degraded = [r for r in reports if r["deepest_tier_name"] == "unconstrained-QEM"]
    if missed or degraded:
        print(f"flowlod: {len(missed)} level(s) missed budget, "
              f"{len(degraded)} fell to unconstrained QEM")
        sys.exit(1)


if __name__ == "__main__":
    main()
