from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import simplecadapi as scad

WORKSPACE = Path(__file__).resolve().parents[2]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from sw2cadir import runtime as rt


OUT_DIR = Path(__file__).resolve().parent
JSON_PATH = (OUT_DIR / 'feature_tree.json').resolve()
NAME = 'upper_bracket_1_featured'
FEATURE_INDICES = [19, 21, 23]


@dataclass
class ExplicitBuildResult:
    part: Any
    report: list[str]
    operations: list[dict[str, Any]]


def build_model() -> ExplicitBuildResult:
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    _, IGNORE_EXTRUDE_REVERSE = rt.prepare_feature_tree(data)
    features_by_index = {feature["index"]: feature for feature in data["features"]}
    report: list[str] = []
    operations: list[dict[str, Any]] = []
    current: Any | None = None

    # Feature 19: Base-Flange1 [SMBaseFlange] -> sheet_metal_base_flange
    feature = features_by_index[19]
    sketch = rt.get_feature_sketch(feature)
    solids = rt.base_flange_from_feature(feature)
    if solids:
        current = rt.safe_union(current, solids, report)
        report.append(f"OK base-flange {feature.get('name')}: solids={len(solids)}")
        operations.append(
            rt.operation_entry(
                feature,
                status="ok",
                rule='sheet_metal_base_flange',
                sketch=sketch,
                simplecadapi_ops=['make_segment_redge/make_three_point_arc_redge', 'make_wire_from_edges_rwire', 'extrude_rsolid', 'union_rsolid'],
                current=current,
                details={'solid_count': len(solids)},
            )
        )
    else:
        report.append(f"SKIP base-flange {feature.get('name')}: unsupported sketch")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule="sheet_metal_base_flange",
                sketch=sketch,
                details={"reason": "unsupported sketch"},
            )
        )

    # Feature 21: Cut-Extrude1 [Cut] -> cut_extrude
    feature = features_by_index[21]
    sketch = rt.get_feature_sketch(feature)
    if sketch is None:
        report.append(f"SKIP {feature.get('name')}: no sketch")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule='cut_extrude',
                details={"reason": "no sketch"},
            )
        )
    elif current is None:
        report.append(f"SKIP {feature.get('name')}: cut before base solid")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule="cut_extrude",
                sketch=sketch,
                details={"reason": "cut before base solid"},
            )
        )
    else:
        solids = rt.sheet_metal_equivalent_normal_cut_tools(
            current, sketch, feature, IGNORE_EXTRUDE_REVERSE, report
        )
        equivalent_normal_cut = solids is not None
        if solids is None:
            solids = rt.auto_extrude_profiles(
                sketch, feature, IGNORE_EXTRUDE_REVERSE, current=current
            )
        current = rt.safe_cut(current, solids, report)
        report.append(f"OK cut-extrude {feature.get('name')}: tools={len(solids)}")
        operations.append(
            rt.operation_entry(
                feature,
                status="ok",
                rule=feature.get("_runtime_normal_cut_rule", "sheet_metal_equivalent_normal_cut")
                if equivalent_normal_cut
                else "cut_extrude",
                sketch=sketch,
                simplecadapi_ops=['make_segment_redge/make_three_point_arc_redge/make_circle_rwire', 'make_wire_from_edges_rwire/contour_wires/profile_wires/region_wires', 'post_state_direction_candidate_match', 'extrude_rsolid', 'cut_rsolid'],
                current=current,
                details={"tool_count": len(solids)},
            )
        )

    # Feature 23: Cut-Extrude2 [Cut] -> cut_extrude
    feature = features_by_index[23]
    sketch = rt.get_feature_sketch(feature)
    if sketch is None:
        report.append(f"SKIP {feature.get('name')}: no sketch")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule='cut_extrude',
                details={"reason": "no sketch"},
            )
        )
    elif current is None:
        report.append(f"SKIP {feature.get('name')}: cut before base solid")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule="cut_extrude",
                sketch=sketch,
                details={"reason": "cut before base solid"},
            )
        )
    else:
        solids = rt.sheet_metal_equivalent_normal_cut_tools(
            current, sketch, feature, IGNORE_EXTRUDE_REVERSE, report
        )
        equivalent_normal_cut = solids is not None
        if solids is None:
            solids = rt.auto_extrude_profiles(
                sketch, feature, IGNORE_EXTRUDE_REVERSE, current=current
            )
        current = rt.safe_cut(current, solids, report)
        report.append(f"OK cut-extrude {feature.get('name')}: tools={len(solids)}")
        operations.append(
            rt.operation_entry(
                feature,
                status="ok",
                rule=feature.get("_runtime_normal_cut_rule", "sheet_metal_equivalent_normal_cut")
                if equivalent_normal_cut
                else "cut_extrude",
                sketch=sketch,
                simplecadapi_ops=['make_segment_redge/make_three_point_arc_redge/make_circle_rwire', 'make_wire_from_edges_rwire/contour_wires/profile_wires/region_wires', 'post_state_direction_candidate_match', 'extrude_rsolid', 'cut_rsolid'],
                current=current,
                details={"tool_count": len(solids)},
            )
        )


    if current is None:
        raise RuntimeError("No solid was generated from explicit feature steps.")
    return ExplicitBuildResult(current, report, operations)


def main() -> None:
    result = build_model()
    scad.export_step(result.part, str(OUT_DIR / f"{NAME}.step"))
    scad.export_stl(result.part, str(OUT_DIR / f"{NAME}.stl"))
    (OUT_DIR / f"{NAME}.build_report.json").write_text(
        json.dumps(
            {
                "source_json": str(JSON_PATH),
                "feature_indices": FEATURE_INDICES,
                "report": result.report,
                "operation_trace": result.operations,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT_DIR / f"{NAME}.operation_trace.json").write_text(
        json.dumps(result.operations, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"exported {NAME}")


if __name__ == "__main__":
    main()
