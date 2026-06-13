from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


GEOMETRY_TYPES = {"Extrusion", "ICE", "Cut", "Revolution", "RevCut", "SMBaseFlange"}


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return s or "model"


def is_cut_feature(feature: dict[str, Any]) -> bool:
    name = feature.get("name", "")
    ftype = feature.get("type", "")
    name_lower = name.lower()
    return ftype in {"Cut", "RevCut"} or "cut" in name_lower or "\u5207\u9664" in name


def geometry_features(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in data.get("features", []) if f.get("type") in GEOMETRY_TYPES]


def block_header(feature: dict[str, Any], rule: str) -> str:
    return (
        f"    # Feature {feature.get('index')}: "
        f"{feature.get('name')} [{feature.get('type')}] -> {rule}\n"
        f"    feature = features_by_index[{feature.get('index')!r}]\n"
        "    sketch = rt.get_feature_sketch(feature)\n"
    )


def append_trace(rule: str, ops: list[str], detail_key: str) -> str:
    return (
        "        operations.append(\n"
        "            rt.operation_entry(\n"
        "                feature,\n"
        "                status=\"ok\",\n"
        f"                rule={rule!r},\n"
        "                sketch=sketch,\n"
        f"                simplecadapi_ops={ops!r},\n"
        "                current=current,\n"
        f"                details={{{detail_key!r}: len(solids)}},\n"
        "            )\n"
        "        )\n"
    )


def skip_no_sketch(rule: str) -> str:
    return (
        "    if sketch is None:\n"
        "        report.append(f\"SKIP {feature.get('name')}: no sketch\")\n"
        "        operations.append(\n"
        "            rt.operation_entry(\n"
        "                feature,\n"
        "                status=\"skipped\",\n"
        f"                rule={rule!r},\n"
        "                details={\"reason\": \"no sketch\"},\n"
        "            )\n"
        "        )\n"
    )


def add_extrude_block(feature: dict[str, Any]) -> str:
    ops = [
        "make_segment_redge/make_three_point_arc_redge/make_circle_rwire",
        "make_wire_from_edges_rwire/contour_wires/profile_wires",
        "post_state_direction_candidate_match",
        "extrude_rsolid",
        "union_rsolid",
    ]
    return (
        block_header(feature, "add_extrude")
        + skip_no_sketch("add_extrude")
        + "    else:\n"
        + "        solids = rt.auto_extrude_profiles(\n"
        + "            sketch, feature, IGNORE_EXTRUDE_REVERSE, current=current\n"
        + "        )\n"
        + "        current = rt.safe_union(\n"
        + "            current,\n"
        + "            solids,\n"
        + "            report,\n"
        + "            mirror_overlap_plane=rt.mirrored_overlap_plane_for_add(sketch, feature),\n"
        + "        )\n"
        + "        report.append(f\"OK add-extrude {feature.get('name')}: solids={len(solids)}\")\n"
        + append_trace("add_extrude", ops, "solid_count")
    )


def cut_extrude_block(feature: dict[str, Any]) -> str:
    ops = [
        "make_segment_redge/make_three_point_arc_redge/make_circle_rwire",
        "make_wire_from_edges_rwire/contour_wires/profile_wires/region_wires",
        "post_state_direction_candidate_match",
        "extrude_rsolid",
        "cut_rsolid",
    ]
    return (
        block_header(feature, "cut_extrude")
        + skip_no_sketch("cut_extrude")
        + "    elif current is None:\n"
        + "        report.append(f\"SKIP {feature.get('name')}: cut before base solid\")\n"
        + "        operations.append(\n"
        + "            rt.operation_entry(\n"
        + "                feature,\n"
        + "                status=\"skipped\",\n"
        + "                rule=\"cut_extrude\",\n"
        + "                sketch=sketch,\n"
        + "                details={\"reason\": \"cut before base solid\"},\n"
        + "            )\n"
        + "        )\n"
        + "    else:\n"
        + "        solids = rt.sheet_metal_equivalent_normal_cut_tools(\n"
        + "            current, sketch, feature, IGNORE_EXTRUDE_REVERSE, report\n"
        + "        )\n"
        + "        equivalent_normal_cut = solids is not None\n"
        + "        if solids is None:\n"
        + "            solids = rt.auto_extrude_profiles(\n"
        + "                sketch, feature, IGNORE_EXTRUDE_REVERSE, current=current\n"
        + "            )\n"
        + "        current = rt.safe_cut(current, solids, report)\n"
        + "        report.append(f\"OK cut-extrude {feature.get('name')}: tools={len(solids)}\")\n"
        + "        operations.append(\n"
        + "            rt.operation_entry(\n"
        + "                feature,\n"
        + "                status=\"ok\",\n"
        + "                rule=feature.get(\"_runtime_normal_cut_rule\", \"sheet_metal_equivalent_normal_cut\")\n"
        + "                if equivalent_normal_cut\n"
        + "                else \"cut_extrude\",\n"
        + "                sketch=sketch,\n"
        + f"                simplecadapi_ops={ops!r},\n"
        + "                current=current,\n"
        + "                details={\"tool_count\": len(solids)},\n"
        + "            )\n"
        + "        )\n"
    )


def add_revolve_block(feature: dict[str, Any]) -> str:
    ops = [
        "make_segment_redge/make_three_point_arc_redge",
        "make_wire_from_edges_rwire/contour_wires/profile_wires",
        "revolve_rsolid",
        "union_rsolid",
    ]
    return (
        block_header(feature, "add_revolve")
        + skip_no_sketch("add_revolve")
        + "    else:\n"
        + "        solids = rt.revolve_profiles(sketch, feature)\n"
        + "        current = rt.safe_union(current, solids, report)\n"
        + "        report.append(f\"OK revolve {feature.get('name')}: solids={len(solids)}\")\n"
        + append_trace("add_revolve", ops, "solid_count")
    )


def revcut_block(feature: dict[str, Any]) -> str:
    ops = [
        "make_segment_redge/make_three_point_arc_redge",
        "make_wire_from_edges_rwire/contour_wires/profile_wires",
        "revolve_rsolid",
        "mirror_shape/post_state_candidate_match",
        "cut_rsolid",
    ]
    return (
        block_header(feature, "revcut")
        + skip_no_sketch("revcut")
        + "    elif current is None:\n"
        + "        report.append(f\"SKIP {feature.get('name')}: revcut before base solid\")\n"
        + "        operations.append(\n"
        + "            rt.operation_entry(\n"
        + "                feature,\n"
        + "                status=\"skipped\",\n"
        + "                rule=\"revcut\",\n"
        + "                sketch=sketch,\n"
        + "                details={\"reason\": \"revcut before base solid\"},\n"
        + "            )\n"
        + "        )\n"
        + "    else:\n"
        + "        solids = rt.revolve_profiles(sketch, feature, current=current)\n"
        + "        current = rt.safe_cut(current, solids, report)\n"
        + "        report.append(f\"OK revcut {feature.get('name')}: tools={len(solids)}\")\n"
        + append_trace("revcut", ops, "tool_count")
    )


def base_flange_block(feature: dict[str, Any]) -> str:
    ops = [
        "make_segment_redge/make_three_point_arc_redge",
        "make_wire_from_edges_rwire",
        "extrude_rsolid",
        "union_rsolid",
    ]
    return (
        block_header(feature, "sheet_metal_base_flange")
        + "    solids = rt.base_flange_from_feature(feature)\n"
        + "    if solids:\n"
        + "        current = rt.safe_union(current, solids, report)\n"
        + "        report.append(f\"OK base-flange {feature.get('name')}: solids={len(solids)}\")\n"
        + append_trace("sheet_metal_base_flange", ops, "solid_count")
        + "    else:\n"
        + "        report.append(f\"SKIP base-flange {feature.get('name')}: unsupported sketch\")\n"
        + "        operations.append(\n"
        + "            rt.operation_entry(\n"
        + "                feature,\n"
        + "                status=\"skipped\",\n"
        + "                rule=\"sheet_metal_base_flange\",\n"
        + "                sketch=sketch,\n"
        + "                details={\"reason\": \"unsupported sketch\"},\n"
        + "            )\n"
        + "        )\n"
    )


def explicit_block(feature: dict[str, Any]) -> str:
    ftype = feature.get("type")
    if ftype == "SMBaseFlange":
        return base_flange_block(feature)
    if ftype in {"Extrusion", "ICE", "Cut"}:
        return cut_extrude_block(feature) if is_cut_feature(feature) else add_extrude_block(feature)
    if ftype == "Revolution":
        return add_revolve_block(feature)
    if ftype == "RevCut":
        return revcut_block(feature)
    raise ValueError(f"Unsupported generated feature type: {ftype}")


def write_model(json_path: Path, out_root: Path, package_name: str = "sw2cadir") -> Path:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    model_slug = slug(data["title"])
    out_dir = out_root / model_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    rel_json = os.path.relpath(json_path.resolve(), out_dir.resolve())
    name = f"{model_slug}_featured"
    blocks = "\n".join(explicit_block(feature) for feature in geometry_features(data))
    feature_indices = [feature.get("index") for feature in geometry_features(data)]

    script = f'''from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import simplecadapi as scad

WORKSPACE = Path(__file__).resolve().parents[2]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from {package_name} import runtime as rt


OUT_DIR = Path(__file__).resolve().parent
JSON_PATH = (OUT_DIR / {rel_json!r}).resolve()
NAME = {name!r}
FEATURE_INDICES = {feature_indices!r}


@dataclass
class ExplicitBuildResult:
    part: Any
    report: list[str]
    operations: list[dict[str, Any]]


def build_model() -> ExplicitBuildResult:
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    _, IGNORE_EXTRUDE_REVERSE = rt.prepare_feature_tree(data)
    features_by_index = {{feature["index"]: feature for feature in data["features"]}}
    report: list[str] = []
    operations: list[dict[str, Any]] = []
    current: Any | None = None

{blocks}

    if current is None:
        raise RuntimeError("No solid was generated from explicit feature steps.")
    return ExplicitBuildResult(current, report, operations)


def main() -> None:
    result = build_model()
    scad.export_step(result.part, str(OUT_DIR / f"{{NAME}}.step"))
    scad.export_stl(result.part, str(OUT_DIR / f"{{NAME}}.stl"))
    (OUT_DIR / f"{{NAME}}.build_report.json").write_text(
        json.dumps(
            {{
                "source_json": str(JSON_PATH),
                "feature_indices": FEATURE_INDICES,
                "report": result.report,
                "operation_trace": result.operations,
            }},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT_DIR / f"{{NAME}}.operation_trace.json").write_text(
        json.dumps(result.operations, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"exported {{NAME}}")


if __name__ == "__main__":
    main()
'''
    (out_dir / "model.py").write_text(script, encoding="utf-8")
    return out_dir / "model.py"


def find_feature_jsons(root: Path) -> list[Path]:
    paths = list(root.glob("*.feature_tree.json"))
    paths.extend(root.glob("*/feature_tree.json"))
    return sorted(set(paths))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate explicit SimpleCADAPI scripts from sw2cadir feature JSON."
    )
    parser.add_argument(
        "--extracted-dir",
        type=Path,
        default=Path("extracted"),
        help="Directory containing *.feature_tree.json files.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("simplecadapi_models_featured"),
        help="Directory for generated model folders.",
    )
    parser.add_argument(
        "--package-name",
        default="sw2cadir",
        help="Python package name used by generated models.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_paths = find_feature_jsons(args.extracted_dir)
    if not json_paths:
        print(f"No feature JSON files found in {args.extracted_dir}", file=sys.stderr)
        sys.exit(1)
    for json_path in json_paths:
        out = write_model(json_path, args.out_dir, args.package_name)
        print(out)


if __name__ == "__main__":
    main()
