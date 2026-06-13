from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_FILES_TO_AUDIT = [
    PACKAGE_DIR / "runtime.py",
    PACKAGE_DIR / "generate_simplecadapi.py",
]
FORBIDDEN_RULE_SNIPPETS = [
    "Cut-Extrude1",
    "Cut-Extrude2",
    "SHFT40b",
    "{37, 40}",
    "{1, 4, 5, 7, 8, 9}",
]

GEOMETRY_TYPES = {"Extrusion", "ICE", "Cut", "Revolution", "RevCut", "SMBaseFlange"}
FEATURE_TYPES_WITH_SELECTION_METADATA = {"Extrusion", "ICE", "Cut", "Revolution", "RevCut"}
SUPPORTED_SEGMENTS = {"line", "arc", "circle"}
REQUIRED_CS = {"origin_model_m", "x_axis_model", "y_axis_model", "z_axis_model"}


def slug(name: str) -> str:
    import re

    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return s or "model"


def sketches_for_feature(feature: dict[str, Any]) -> list[dict[str, Any]]:
    sketches = []
    if "sketch" in feature:
        sketches.append(feature["sketch"])
    for sub in feature.get("subfeatures", []):
        if "sketch" in sub:
            sketches.append(sub["sketch"])
    return sketches


def require(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def audit_segment(model_name: str, feature: dict[str, Any], segment: dict[str, Any], failures: list[str]) -> None:
    prefix = f"{model_name} feature {feature.get('index')} {feature.get('name')}"
    kind = segment.get("kind")
    require(kind in SUPPORTED_SEGMENTS, f"{prefix}: unsupported segment kind {kind!r}", failures)
    if kind == "line":
        for key in ("start_sketch_m", "end_sketch_m", "start_model_m", "end_model_m"):
            require(key in segment, f"{prefix}: line missing {key}", failures)
    elif kind == "arc":
        for key in (
            "start_sketch_m",
            "end_sketch_m",
            "center_sketch_m",
            "start_model_m",
            "end_model_m",
            "center_model_m",
            "radius_m",
        ):
            require(key in segment, f"{prefix}: arc missing {key}", failures)
    elif kind == "circle":
        for key in ("center_sketch_m", "center_model_m", "radius_m"):
            require(key in segment, f"{prefix}: circle missing {key}", failures)


def audit_sketch_contours(model_name: str, feature: dict[str, Any], sketch: dict[str, Any], failures: list[str]) -> None:
    prefix = f"{model_name} feature {feature.get('index')} {feature.get('name')}"
    contours = sketch.get("contours")
    require(isinstance(contours, list), f"{prefix}: missing sketch contours export", failures)
    if not isinstance(contours, list):
        return
    contour_count = sketch.get("contour_count")
    if contour_count is not None:
        require(
            int(contour_count) == len(contours),
            f"{prefix}: contour_count {contour_count} != exported contours {len(contours)}",
            failures,
        )
    for contour in contours:
        require("edges" in contour, f"{prefix}: contour {contour.get('index')} missing edges", failures)
        require("segments" in contour, f"{prefix}: contour {contour.get('index')} missing segments", failures)
        for segment in contour.get("segments", []):
            audit_segment(model_name, feature, segment, failures)


def audit_selection_metadata(model_name: str, feature: dict[str, Any], failures: list[str]) -> None:
    if feature.get("type") not in FEATURE_TYPES_WITH_SELECTION_METADATA:
        return
    prefix = f"{model_name} feature {feature.get('index')} {feature.get('name')}"
    metadata = feature.get("definition", {}).get("selection_metadata")
    require(isinstance(metadata, dict), f"{prefix}: missing SOLIDWORKS selection metadata", failures)
    if not isinstance(metadata, dict):
        return
    require("selected_contours_count" in metadata, f"{prefix}: selection metadata missing selected_contours_count", failures)
    require("selection_manager_after_access" in metadata, f"{prefix}: selection metadata missing selection manager snapshot", failures)
    count = metadata.get("selected_contours_count")
    selected_contours = metadata.get("selected_contours")
    require(isinstance(selected_contours, list), f"{prefix}: selection metadata missing selected_contours list", failures)
    if isinstance(count, int) and count > 0 and isinstance(selected_contours, list):
        require(
            len(selected_contours) == count,
            f"{prefix}: selected_contours length {len(selected_contours)} != count {count}",
            failures,
        )
        for contour in selected_contours:
            require("edges" in contour, f"{prefix}: selected contour {contour.get('index')} missing edges", failures)
    scope_metadata = feature.get("definition", {}).get("feature_scope_metadata")
    require(isinstance(scope_metadata, dict), f"{prefix}: missing feature scope metadata", failures)
    if isinstance(scope_metadata, dict):
        require("feature_scope_bodies_count" in scope_metadata, f"{prefix}: scope metadata missing body count", failures)
        bodies = scope_metadata.get("feature_scope_bodies")
        require(isinstance(bodies, list), f"{prefix}: scope metadata missing body list", failures)
        count = scope_metadata.get("feature_scope_bodies_count")
        if isinstance(count, int) and count > 0 and isinstance(bodies, list):
            require(
                len(bodies) == count,
                f"{prefix}: scope body list length {len(bodies)} != count {count}",
                failures,
            )
            for body in bodies:
                require("name" in body, f"{prefix}: scope body missing name", failures)
                require("body_box_m" in body, f"{prefix}: scope body missing body_box_m", failures)


def audit_feature_tree(path: Path, failures: list[str]) -> tuple[str, list[int]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    model_name = data.get("title", path.stem)
    hints = data.get("conversion_hints") or {}
    for forbidden_hint in (
        "revolve_cut_mirror_feature_indices",
        "revolve_cut_mirror_planes",
        "ignore_extrude_reverse",
    ):
        require(
            forbidden_hint not in hints,
            f"{model_name}: conversion_hints still contains deprecated compatibility hint {forbidden_hint}",
            failures,
        )
    body_indices = []
    for feature in data.get("features", []):
        if feature.get("type") in GEOMETRY_TYPES:
            body_indices.append(int(feature["index"]))
            post_state = feature.get("solidworks_post_state")
            require(
                isinstance(post_state, dict) and "volume_mm3" in post_state,
                f"{model_name} feature {feature.get('index')}: missing SOLIDWORKS post-state MassProperty",
                failures,
            )
        audit_selection_metadata(model_name, feature, failures)
        for sketch in sketches_for_feature(feature):
            cs = sketch.get("coordinate_system")
            require(isinstance(cs, dict), f"{model_name} feature {feature.get('index')}: missing coordinate_system", failures)
            if isinstance(cs, dict):
                missing = REQUIRED_CS - set(cs)
                require(not missing, f"{model_name} feature {feature.get('index')}: coordinate_system missing {sorted(missing)}", failures)
            require(sketch.get("sketch_to_model_matrix") is not None, f"{model_name} feature {feature.get('index')}: missing sketch matrix", failures)
            for segment in sketch.get("segments", []):
                audit_segment(model_name, feature, segment, failures)
            audit_sketch_contours(model_name, feature, sketch, failures)
    return model_name, body_indices


def audit_operation_trace(
    model_name: str,
    body_indices: list[int],
    models_dir: Path,
    failures: list[str],
) -> None:
    trace_path = models_dir / slug(model_name) / f"{slug(model_name)}_featured.operation_trace.json"
    require(trace_path.exists(), f"{model_name}: missing operation trace {trace_path}", failures)
    if not trace_path.exists():
        return
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    traced_indices = [int(row["feature_index"]) for row in trace]
    require(traced_indices == body_indices, f"{model_name}: traced indices {traced_indices} != body indices {body_indices}", failures)
    for row in trace:
        require(row.get("status") == "ok", f"{model_name} feature {row.get('feature_index')}: trace status {row.get('status')}", failures)
        require(row.get("rule"), f"{model_name} feature {row.get('feature_index')}: missing conversion rule", failures)
        require(row.get("simplecadapi_ops"), f"{model_name} feature {row.get('feature_index')}: missing SimpleCADAPI ops", failures)
        sketch = row.get("sketch") or {}
        require(
            isinstance(sketch.get("runtime_coordinate_system"), dict),
            f"{model_name} feature {row.get('feature_index')}: missing runtime coordinate system in trace",
            failures,
        )
        counts = (sketch.get("segment_counts") or {})
        require(counts.get("unsupported", 0) == 0, f"{model_name} feature {row.get('feature_index')}: unsupported segments in trace", failures)
        if row.get("feature_type") in FEATURE_TYPES_WITH_SELECTION_METADATA:
            details = row.get("details") or {}
            require(
                isinstance(details.get("solidworks_selection"), dict),
                f"{model_name} feature {row.get('feature_index')}: trace missing SOLIDWORKS selection metadata",
                failures,
            )
            require(
                isinstance(details.get("solidworks_feature_scope"), dict),
                f"{model_name} feature {row.get('feature_index')}: trace missing SOLIDWORKS feature scope metadata",
                failures,
            )
            if "region_selection" not in details:
                require(
                    isinstance(details.get("profile_selection"), dict),
                    f"{model_name} feature {row.get('feature_index')}: trace missing profile source selection",
                    failures,
                )
        if row.get("status") == "ok":
            details = row.get("details") or {}
            require(
                isinstance(details.get("simplecadapi_post_state"), dict)
                and "volume_mm3" in details.get("simplecadapi_post_state", {}),
                f"{model_name} feature {row.get('feature_index')}: trace missing SimpleCADAPI post-state",
                failures,
            )
            require(
                isinstance(details.get("solidworks_post_state"), dict)
                and "volume_mm3" in details.get("solidworks_post_state", {}),
                f"{model_name} feature {row.get('feature_index')}: trace missing SOLIDWORKS post-state",
                failures,
            )
            require(
                isinstance(details.get("post_state_delta"), dict)
                and "volume_mm3" in details.get("post_state_delta", {}),
                f"{model_name} feature {row.get('feature_index')}: trace missing post-state volume delta",
                failures,
            )


def audit_rule_generalization(failures: list[str], source_files: list[Path]) -> None:
    for source_path in source_files:
        text = source_path.read_text(encoding="utf-8")
        for snippet in FORBIDDEN_RULE_SNIPPETS:
            require(
                snippet not in text,
                f"{source_path.name}: converter rule contains hard-coded model/feature snippet {snippet!r}",
                failures,
            )


def find_feature_jsons(root: Path) -> list[Path]:
    paths = list(root.glob("*.feature_tree.json"))
    paths.extend(root.glob("*/feature_tree.json"))
    return sorted(set(paths))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit extracted feature JSON and generated sw2cadir operation traces."
    )
    parser.add_argument(
        "--extracted-dir",
        type=Path,
        default=Path("extracted"),
        help="Directory containing *.feature_tree.json files.",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path("simplecadapi_models_featured"),
        help="Directory containing generated model folders and operation traces.",
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        action="append",
        dest="source_files",
        help="Converter source file to scan for hard-coded special cases. May be repeated.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    failures: list[str] = []
    summaries = []
    audit_rule_generalization(failures, args.source_files or DEFAULT_SOURCE_FILES_TO_AUDIT)
    json_paths = find_feature_jsons(args.extracted_dir)
    require(bool(json_paths), f"no feature JSON files found in {args.extracted_dir}", failures)
    for json_path in json_paths:
        model_name, body_indices = audit_feature_tree(json_path, failures)
        audit_operation_trace(model_name, body_indices, args.models_dir, failures)
        summaries.append((model_name, len(body_indices), body_indices))

    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        sys.exit(1)

    for model_name, count, indices in summaries:
        print(f"OK {model_name}: {count} geometry features traced {indices}")


if __name__ == "__main__":
    main()
