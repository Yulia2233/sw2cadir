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
NAME = 'shft40b_featured'
FEATURE_INDICES = [19, 22, 25, 28, 31, 34, 37, 40, 43, 46]


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

    # Feature 19: 凸台-拉伸1 [Extrusion] -> add_extrude
    feature = features_by_index[19]
    sketch = rt.get_feature_sketch(feature)
    if sketch is None:
        report.append(f"SKIP {feature.get('name')}: no sketch")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule='add_extrude',
                details={"reason": "no sketch"},
            )
        )
    else:
        solids = rt.auto_extrude_profiles(
            sketch, feature, IGNORE_EXTRUDE_REVERSE, current=current
        )
        current = rt.safe_union(
            current,
            solids,
            report,
            mirror_overlap_plane=rt.mirrored_overlap_plane_for_add(sketch, feature),
        )
        report.append(f"OK add-extrude {feature.get('name')}: solids={len(solids)}")
        operations.append(
            rt.operation_entry(
                feature,
                status="ok",
                rule='add_extrude',
                sketch=sketch,
                simplecadapi_ops=['make_segment_redge/make_three_point_arc_redge/make_circle_rwire', 'make_wire_from_edges_rwire/contour_wires/profile_wires', 'post_state_direction_candidate_match', 'extrude_rsolid', 'union_rsolid'],
                current=current,
                details={'solid_count': len(solids)},
            )
        )

    # Feature 22: 旋转1 [Revolution] -> add_revolve
    feature = features_by_index[22]
    sketch = rt.get_feature_sketch(feature)
    if sketch is None:
        report.append(f"SKIP {feature.get('name')}: no sketch")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule='add_revolve',
                details={"reason": "no sketch"},
            )
        )
    else:
        solids = rt.revolve_profiles(sketch, feature)
        current = rt.safe_union(current, solids, report)
        report.append(f"OK revolve {feature.get('name')}: solids={len(solids)}")
        operations.append(
            rt.operation_entry(
                feature,
                status="ok",
                rule='add_revolve',
                sketch=sketch,
                simplecadapi_ops=['make_segment_redge/make_three_point_arc_redge', 'make_wire_from_edges_rwire/contour_wires/profile_wires', 'revolve_rsolid', 'union_rsolid'],
                current=current,
                details={'solid_count': len(solids)},
            )
        )

    # Feature 25: 凸台-拉伸2 [ICE] -> add_extrude
    feature = features_by_index[25]
    sketch = rt.get_feature_sketch(feature)
    if sketch is None:
        report.append(f"SKIP {feature.get('name')}: no sketch")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule='add_extrude',
                details={"reason": "no sketch"},
            )
        )
    else:
        solids = rt.auto_extrude_profiles(
            sketch, feature, IGNORE_EXTRUDE_REVERSE, current=current
        )
        current = rt.safe_union(
            current,
            solids,
            report,
            mirror_overlap_plane=rt.mirrored_overlap_plane_for_add(sketch, feature),
        )
        report.append(f"OK add-extrude {feature.get('name')}: solids={len(solids)}")
        operations.append(
            rt.operation_entry(
                feature,
                status="ok",
                rule='add_extrude',
                sketch=sketch,
                simplecadapi_ops=['make_segment_redge/make_three_point_arc_redge/make_circle_rwire', 'make_wire_from_edges_rwire/contour_wires/profile_wires', 'post_state_direction_candidate_match', 'extrude_rsolid', 'union_rsolid'],
                current=current,
                details={'solid_count': len(solids)},
            )
        )

    # Feature 28: 切除-拉伸1 [ICE] -> cut_extrude
    feature = features_by_index[28]
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

    # Feature 31: 切除-拉伸2 [ICE] -> cut_extrude
    feature = features_by_index[31]
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

    # Feature 34: 切除-旋转1 [RevCut] -> revcut
    feature = features_by_index[34]
    sketch = rt.get_feature_sketch(feature)
    if sketch is None:
        report.append(f"SKIP {feature.get('name')}: no sketch")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule='revcut',
                details={"reason": "no sketch"},
            )
        )
    elif current is None:
        report.append(f"SKIP {feature.get('name')}: revcut before base solid")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule="revcut",
                sketch=sketch,
                details={"reason": "revcut before base solid"},
            )
        )
    else:
        solids = rt.revolve_profiles(sketch, feature, current=current)
        current = rt.safe_cut(current, solids, report)
        report.append(f"OK revcut {feature.get('name')}: tools={len(solids)}")
        operations.append(
            rt.operation_entry(
                feature,
                status="ok",
                rule='revcut',
                sketch=sketch,
                simplecadapi_ops=['make_segment_redge/make_three_point_arc_redge', 'make_wire_from_edges_rwire/contour_wires/profile_wires', 'revolve_rsolid', 'mirror_shape/post_state_candidate_match', 'cut_rsolid'],
                current=current,
                details={'tool_count': len(solids)},
            )
        )

    # Feature 37: 切除-旋转2 [RevCut] -> revcut
    feature = features_by_index[37]
    sketch = rt.get_feature_sketch(feature)
    if sketch is None:
        report.append(f"SKIP {feature.get('name')}: no sketch")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule='revcut',
                details={"reason": "no sketch"},
            )
        )
    elif current is None:
        report.append(f"SKIP {feature.get('name')}: revcut before base solid")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule="revcut",
                sketch=sketch,
                details={"reason": "revcut before base solid"},
            )
        )
    else:
        solids = rt.revolve_profiles(sketch, feature, current=current)
        current = rt.safe_cut(current, solids, report)
        report.append(f"OK revcut {feature.get('name')}: tools={len(solids)}")
        operations.append(
            rt.operation_entry(
                feature,
                status="ok",
                rule='revcut',
                sketch=sketch,
                simplecadapi_ops=['make_segment_redge/make_three_point_arc_redge', 'make_wire_from_edges_rwire/contour_wires/profile_wires', 'revolve_rsolid', 'mirror_shape/post_state_candidate_match', 'cut_rsolid'],
                current=current,
                details={'tool_count': len(solids)},
            )
        )

    # Feature 40: 切除-旋转3 [RevCut] -> revcut
    feature = features_by_index[40]
    sketch = rt.get_feature_sketch(feature)
    if sketch is None:
        report.append(f"SKIP {feature.get('name')}: no sketch")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule='revcut',
                details={"reason": "no sketch"},
            )
        )
    elif current is None:
        report.append(f"SKIP {feature.get('name')}: revcut before base solid")
        operations.append(
            rt.operation_entry(
                feature,
                status="skipped",
                rule="revcut",
                sketch=sketch,
                details={"reason": "revcut before base solid"},
            )
        )
    else:
        solids = rt.revolve_profiles(sketch, feature, current=current)
        current = rt.safe_cut(current, solids, report)
        report.append(f"OK revcut {feature.get('name')}: tools={len(solids)}")
        operations.append(
            rt.operation_entry(
                feature,
                status="ok",
                rule='revcut',
                sketch=sketch,
                simplecadapi_ops=['make_segment_redge/make_three_point_arc_redge', 'make_wire_from_edges_rwire/contour_wires/profile_wires', 'revolve_rsolid', 'mirror_shape/post_state_candidate_match', 'cut_rsolid'],
                current=current,
                details={'tool_count': len(solids)},
            )
        )

    # Feature 43: 切除-拉伸3 [ICE] -> cut_extrude
    feature = features_by_index[43]
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

    # Feature 46: 切除-拉伸4 [ICE] -> cut_extrude
    feature = features_by_index[46]
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
