from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import simplecadapi as scad


MM = 1000.0
BEND_NORMAL_CUT_SAMPLES = 64


def v3(values: Any) -> np.ndarray:
    return np.array([float(values[0]), float(values[1]), float(values[2])], dtype=float)


def unit(values: Any) -> tuple[float, float, float]:
    arr = v3(values)
    n = float(np.linalg.norm(arr))
    if n < 1e-12:
        return (0.0, 0.0, 1.0)
    arr = arr / n
    return (float(arr[0]), float(arr[1]), float(arr[2]))


def mm_point(values: Any) -> tuple[float, float, float]:
    arr = v3(values) * MM
    return (float(arr[0]), float(arr[1]), float(arr[2]))


def point_key(values: Any, tol: float = 1e-7) -> tuple[int, int, int]:
    arr = v3(values)
    return tuple(int(round(float(x) / tol)) for x in arr)


def transform_sketch_point(sketch: dict[str, Any], point_m: Any) -> tuple[float, float, float]:
    matrix = sketch.get("runtime_matrix") or sketch.get("sketch_to_model_matrix")
    if not matrix:
        return mm_point(point_m)
    m = np.array(matrix, dtype=float)
    p = np.array([float(point_m[0]), float(point_m[1]), float(point_m[2]), 1.0])
    out = m @ p
    return mm_point(out[:3])


def sketch_normal(sketch: dict[str, Any]) -> tuple[float, float, float]:
    matrix = sketch.get("runtime_matrix") or sketch.get("sketch_to_model_matrix")
    if not matrix:
        return (0.0, 0.0, 1.0)
    m = np.array(matrix, dtype=float)
    return unit(m[:3, 2])


def sketch_reference_point_m(sketch: dict[str, Any]) -> np.ndarray:
    for segment in sketch.get("segments", []):
        if "start_model_m" in segment:
            return v3(segment["start_model_m"])
        if "start_sketch_m" in segment:
            return np.array(transform_sketch_point(sketch, segment["start_sketch_m"]), dtype=float) / MM
    return np.zeros(3, dtype=float)


def reverse_dir(vec: tuple[float, float, float]) -> tuple[float, float, float]:
    return (-vec[0], -vec[1], -vec[2])


def coordinate_system_from_matrix(matrix: Any) -> dict[str, Any]:
    m = np.array(matrix, dtype=float)
    return {
        "origin_model_m": [float(x) for x in m[:3, 3]],
        "x_axis_model": list(unit(m[:3, 0])),
        "y_axis_model": list(unit(m[:3, 1])),
        "z_axis_model": list(unit(m[:3, 2])),
    }


def solid_list(shape: Any) -> list[Any]:
    return shape if isinstance(shape, list) else [shape]


def shape_volume(shape: Any) -> float:
    return sum(float(solid.get_volume()) for solid in solid_list(shape))


def shape_body_count(shape: Any) -> int:
    return len(solid_list(shape))


def shape_surface_area(shape: Any) -> float:
    return sum(sum(float(face.get_area()) for face in solid.get_faces()) for solid in solid_list(shape))


def shape_center_mm(shape: Any) -> list[float] | None:
    try:
        from simplecadapi.kernel.ocp_properties import center_of_mass
    except Exception:
        return None
    solids = solid_list(shape)
    volumes = [float(solid.get_volume()) for solid in solids]
    total_volume = sum(volumes)
    if total_volume <= 1e-12:
        return None
    centers = [np.array(center_of_mass(solid.wrapped).to_tuple(), dtype=float) for solid in solids]
    weighted = sum(center * volume for center, volume in zip(centers, volumes)) / total_volume
    return [float(x) for x in weighted]


def shape_bbox_mm(shape: Any) -> tuple[float, float, float, float, float, float] | None:
    try:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
    except Exception:
        return None
    try:
        box = Bnd_Box()
        for solid in solid_list(shape):
            BRepBndLib.Add_s(solid.wrapped, box)
        return tuple(float(x) for x in box.Get())
    except Exception:
        return None


def shape_post_state(shape: Any) -> dict[str, Any]:
    bbox = shape_bbox_mm(shape)
    row: dict[str, Any] = {
        "volume_mm3": shape_volume(shape),
        "surface_area_mm2": shape_surface_area(shape),
        "center_mm": shape_center_mm(shape),
        "body_count": shape_body_count(shape),
    }
    if bbox is not None:
        row["bbox_mm"] = list(bbox)
        row["span_mm"] = [bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2]]
    return row


def post_state_delta(api_post_state: dict[str, Any], sw_post_state: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key in ("volume_mm3", "surface_area_mm2"):
        if key in api_post_state and key in sw_post_state:
            delta[key] = float(api_post_state[key]) - float(sw_post_state[key])
    for key in ("center_mm", "span_mm", "bbox_mm"):
        if key in api_post_state and key in sw_post_state and api_post_state[key] is not None:
            delta[key] = [
                float(api_post_state[key][index]) - float(sw_post_state[key][index])
                for index in range(min(len(api_post_state[key]), len(sw_post_state[key])))
            ]
    return delta


def post_state_match_score(shape: Any, sw_post_state: dict[str, Any]) -> tuple[float, float, float]:
    if not isinstance(sw_post_state, dict) or "volume_mm3" not in sw_post_state:
        return (math.inf, math.inf, math.inf)
    api_post_state = shape_post_state(shape)
    delta = post_state_delta(api_post_state, sw_post_state)
    volume_score = abs(float(delta.get("volume_mm3", math.inf)))
    center_delta = delta.get("center_mm")
    center_score = (
        sum(abs(float(value)) for value in center_delta)
        if isinstance(center_delta, list)
        else math.inf
    )
    span_delta = delta.get("span_mm")
    span_score = (
        sum(abs(float(value)) for value in span_delta)
        if isinstance(span_delta, list)
        else math.inf
    )
    return (volume_score, center_score, span_score)


def plane_limited_direction_and_depth(
    sketch: dict[str, Any], normal: tuple[float, float, float], plane_params: Any
) -> tuple[tuple[float, float, float], float] | None:
    if not plane_params or len(plane_params) < 6:
        return None
    plane_normal = v3(plane_params[:3])
    plane_point = v3(plane_params[3:6])
    sketch_point = sketch_reference_point_m(sketch)
    for direction in (normal, reverse_dir(normal)):
        d = v3(direction)
        denom = float(np.dot(plane_normal, d))
        if abs(denom) < 1e-12:
            continue
        depth = float(np.dot(plane_normal, plane_point - sketch_point) / denom)
        if depth > 1e-9 and math.isfinite(depth):
            return (unit(direction), depth)
    return None


def rotate_about_axis(vec: Any, axis: Any, angle_rad: float) -> np.ndarray:
    v = v3(vec)
    k = v3(axis)
    norm = float(np.linalg.norm(k))
    if norm < 1e-12:
        return v
    k = k / norm
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    return v * cos_a + np.cross(k, v) * sin_a + k * float(np.dot(k, v)) * (1.0 - cos_a)


def is_cut_feature(feature: dict[str, Any]) -> bool:
    name = feature.get("name", "")
    ftype = feature.get("type", "")
    name_lower = name.lower()
    return ftype in {"Cut", "RevCut"} or "cut" in name_lower or "\u5207\u9664" in name


def is_add_feature(feature: dict[str, Any]) -> bool:
    return feature.get("type") in {"Extrusion", "Revolution", "ICE", "SMBaseFlange"} and not is_cut_feature(feature)


def get_feature_sketch(feature: dict[str, Any]) -> dict[str, Any] | None:
    for sub in feature.get("subfeatures", []):
        if sub.get("type") == "ProfileFeature" and "sketch" in sub:
            return sub["sketch"]
    return None


def circle_segments(sketch: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        s
        for s in sketch.get("segments", [])
        if s.get("kind") == "circle" and not s.get("construction", False)
    ]


def chain_loops_from_segments(segments: list[dict[str, Any]]) -> list[list[tuple[dict[str, Any], bool]]]:
    candidates = [
        s
        for s in segments
        if s.get("kind") in {"line", "arc"} and not s.get("construction", False)
    ]
    unused = set(range(len(candidates)))
    loops: list[list[tuple[dict[str, Any], bool]]] = []

    while unused:
        first_i = min(unused)
        unused.remove(first_i)
        first = candidates[first_i]
        start_key = point_key(first["start_sketch_m"])
        current_key = point_key(first["end_sketch_m"])
        loop: list[tuple[dict[str, Any], bool]] = [(first, False)]

        guard = 0
        while current_key != start_key and unused and guard < 500:
            found = None
            found_reversed = False
            for i in list(unused):
                seg = candidates[i]
                if point_key(seg["start_sketch_m"]) == current_key:
                    found = i
                    found_reversed = False
                    break
                if point_key(seg["end_sketch_m"]) == current_key:
                    found = i
                    found_reversed = True
                    break
            if found is None:
                break
            unused.remove(found)
            seg = candidates[found]
            loop.append((seg, found_reversed))
            current_key = point_key(seg["start_sketch_m"] if found_reversed else seg["end_sketch_m"])
            guard += 1

        if current_key == start_key and len(loop) >= 2:
            loops.append(loop)

    return loops


def chain_loops(sketch: dict[str, Any]) -> list[list[tuple[dict[str, Any], bool]]]:
    return chain_loops_from_segments(sketch.get("segments", []))


def arc_middle_sketch(seg: dict[str, Any], reversed_segment: bool) -> tuple[float, float, float]:
    start = v3(seg["end_sketch_m"] if reversed_segment else seg["start_sketch_m"])
    end = v3(seg["start_sketch_m"] if reversed_segment else seg["end_sketch_m"])
    center = v3(seg["center_sketch_m"])
    radius = float(seg["radius_m"])
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    rot = int(seg.get("rotation_dir") or 1)
    if reversed_segment:
        rot *= -1
    if rot >= 0:
        delta = (a1 - a0) % (2.0 * math.pi)
        mid = a0 + delta / 2.0
    else:
        delta = (a0 - a1) % (2.0 * math.pi)
        mid = a0 - delta / 2.0
    p = center + np.array([math.cos(mid) * radius, math.sin(mid) * radius, 0.0])
    return (float(p[0]), float(p[1]), float(p[2]))


def edge_from_segment(sketch: dict[str, Any], seg: dict[str, Any], reversed_segment: bool):
    if seg.get("kind") == "line":
        start_key = "end_sketch_m" if reversed_segment else "start_sketch_m"
        end_key = "start_sketch_m" if reversed_segment else "end_sketch_m"
        return scad.make_segment_redge(
            transform_sketch_point(sketch, seg[start_key]),
            transform_sketch_point(sketch, seg[end_key]),
        )
    if seg.get("kind") == "arc":
        start = seg["end_sketch_m"] if reversed_segment else seg["start_sketch_m"]
        end = seg["start_sketch_m"] if reversed_segment else seg["end_sketch_m"]
        middle = arc_middle_sketch(seg, reversed_segment)
        return scad.make_three_point_arc_redge(
            transform_sketch_point(sketch, start),
            transform_sketch_point(sketch, middle),
            transform_sketch_point(sketch, end),
        )
    raise ValueError(f"Unsupported segment kind: {seg.get('kind')}")


def contour_segment_wires(sketch: dict[str, Any], contours: list[dict[str, Any]]) -> tuple[list[Any], list[int]]:
    normal = sketch_normal(sketch)
    wires = []
    used_indices = []
    for contour in contours:
        segments = [
            s
            for s in contour.get("segments", [])
            if s.get("kind") in {"line", "arc", "circle"} and not s.get("construction", False)
        ]
        if not segments:
            continue

        contour_wires = []
        circle_only = [s for s in segments if s.get("kind") == "circle"]
        line_arc = [s for s in segments if s.get("kind") in {"line", "arc"}]
        if circle_only and not line_arc:
            for circle in circle_only:
                contour_wires.append(
                    scad.make_circle_rwire(
                        transform_sketch_point(sketch, circle["center_sketch_m"]),
                        float(circle["radius_m"]) * MM,
                        normal,
                    )
                )
        elif line_arc:
            loops = chain_loops_from_segments(line_arc)
            used_segment_count = sum(len(loop) for loop in loops)
            if loops and used_segment_count == len(line_arc):
                for loop in loops:
                    edges = [edge_from_segment(sketch, seg, rev) for seg, rev in loop]
                    contour_wires.append(scad.make_wire_from_edges_rwire(edges))

        if contour_wires:
            wires.extend(contour_wires)
            used_indices.append(int(contour.get("index", len(used_indices))))
    return wires, used_indices


def contour_edge_wires(contours: list[dict[str, Any]]) -> tuple[list[Any], list[int]]:
    wires = []
    used_indices = []
    for contour in contours:
        edges_data = [
            edge
            for edge in contour.get("edges", [])
            if "start_model_m" in edge and "end_model_m" in edge
        ]
        if not edges_data:
            continue
        edges = [region_edge_to_redge(edge) for edge in edges_data]
        try:
            wires.append(scad.make_wire_from_edges_rwire(edges))
            used_indices.append(int(contour.get("index", len(used_indices))))
        except Exception:
            if len(edges_data) < 2:
                continue
            points = [mm_point(edge["start_model_m"]) for edge in edges_data]
            try:
                wires.append(scad.make_polyline_rwire(points, closed=True))
                used_indices.append(int(contour.get("index", len(used_indices))))
            except Exception:
                pass
    return wires, used_indices


def sketch_contour_wires(
    sketch: dict[str, Any], feature: dict[str, Any] | None = None
) -> list[Any] | None:
    result = sketch_contour_wire_result(sketch)
    if result is None:
        return None
    wires, details = result
    if feature is not None and not feature.get("_runtime_profile_selection"):
        feature["_runtime_profile_selection"] = details
    return wires


def sketch_contour_wire_result(sketch: dict[str, Any]) -> tuple[list[Any], dict[str, Any]] | None:
    contours = sketch.get("contours") or []
    if not contours:
        return None

    wires, used_indices = contour_segment_wires(sketch, contours)
    policy = "solidworks_sketch_contour_segments"
    if not wires:
        wires, used_indices = contour_edge_wires(contours)
        policy = "solidworks_sketch_contour_edges"

    if not wires:
        return None
    return wires, {
        "policy": policy,
        "used_contour_indices": used_indices,
        "exported_contours_count": len(contours),
    }


def legacy_profile_wires(sketch: dict[str, Any]):
    normal = sketch_normal(sketch)
    wires = []
    for circle in circle_segments(sketch):
        wires.append(
            scad.make_circle_rwire(
                transform_sketch_point(sketch, circle["center_sketch_m"]),
                float(circle["radius_m"]) * MM,
                normal,
            )
        )
    for loop in chain_loops(sketch):
        edges = [edge_from_segment(sketch, seg, rev) for seg, rev in loop]
        wires.append(scad.make_wire_from_edges_rwire(edges))
    return wires


def profile_wires(sketch: dict[str, Any], feature: dict[str, Any] | None = None):
    contour_result = sketch_contour_wire_result(sketch)
    legacy_wires = legacy_profile_wires(sketch)
    if contour_result is not None:
        contour_wires, contour_details = contour_result
        if not legacy_wires or len(contour_wires) == len(legacy_wires):
            if feature is not None and not feature.get("_runtime_profile_selection"):
                feature["_runtime_profile_selection"] = contour_details
            return contour_wires
        if feature is not None and not feature.get("_runtime_profile_selection"):
            feature["_runtime_profile_selection"] = {
                "policy": "legacy_segment_chains_after_contour_wire_count_mismatch",
                "legacy_wire_count": len(legacy_wires),
                "contour_wire_count": len(contour_wires),
                "contour_policy": contour_details["policy"],
                "used_contour_indices": contour_details["used_contour_indices"],
                "exported_contours_count": contour_details["exported_contours_count"],
            }
        return legacy_wires

    if feature is not None and legacy_wires and not feature.get("_runtime_profile_selection"):
        feature["_runtime_profile_selection"] = {
            "policy": "legacy_segment_chains_without_solidworks_contours",
            "legacy_wire_count": len(legacy_wires),
        }
    return legacy_wires


def region_wires(sketch: dict[str, Any], indices: set[int] | None = None):
    wires = []
    for region_index, region in enumerate(sketch.get("regions", [])):
        if indices is not None and region_index not in indices:
            continue
        edges_data = [e for e in region.get("edges", []) if "start_model_m" in e and "end_model_m" in e]
        if len(edges_data) < 2:
            continue
        edges = []
        for edge in edges_data:
            edges.append(region_edge_to_redge(edge))
        try:
            wires.append(scad.make_wire_from_edges_rwire(edges))
        except Exception:
            points = [mm_point(edge["start_model_m"]) for edge in edges_data]
            wires.append(scad.make_polyline_rwire(points, closed=True))
    return wires


def selected_contour_wires(feature: dict[str, Any]) -> list[Any] | None:
    metadata = feature.get("definition", {}).get("selection_metadata") or {}
    selected_count = metadata.get("selected_contours_count")
    contours = metadata.get("selected_contours") or []
    if not selected_count or selected_count <= 0 or not contours:
        return None

    wires = []
    contour_indices = []
    for contour in contours:
        edges_data = [edge for edge in contour.get("edges", []) if "start_model_m" in edge and "end_model_m" in edge]
        if len(edges_data) < 1:
            continue
        edges = []
        for edge in edges_data:
            edges.append(region_edge_to_redge(edge))
        try:
            wires.append(scad.make_wire_from_edges_rwire(edges))
            contour_indices.append(contour.get("index"))
        except Exception:
            points = [mm_point(edge["start_model_m"]) for edge in edges_data]
            try:
                wires.append(scad.make_polyline_rwire(points, closed=True))
                contour_indices.append(contour.get("index"))
            except Exception:
                pass

    if not wires:
        return None
    feature["_runtime_profile_selection"] = {
        "policy": "solidworks_selected_contours",
        "selected_contours_count": selected_count,
        "used_contour_indices": contour_indices,
    }
    return wires


def region_metric_mm(region: dict[str, Any]) -> dict[str, Any] | None:
    points = []
    for edge in region.get("edges", []):
        if "start_model_m" in edge:
            points.append(v3(edge["start_model_m"]) * MM)
    if not points:
        return None
    arr = np.array(points, dtype=float)
    spans = arr.max(axis=0) - arr.min(axis=0)
    axes = sorted(range(3), key=lambda axis: spans[axis], reverse=True)[:2]
    area = 0.0
    for i, point in enumerate(arr):
        nxt = arr[(i + 1) % len(arr)]
        area += float(point[axes[0]] * nxt[axes[1]] - nxt[axes[0]] * point[axes[1]])
    return {
        "area_mm2": abs(area) / 2.0,
        "centroid_mm": arr.mean(axis=0).tolist(),
        "bbox_mm": [
            float(arr[:, 0].min()),
            float(arr[:, 1].min()),
            float(arr[:, 2].min()),
            float(arr[:, 0].max()),
            float(arr[:, 1].max()),
            float(arr[:, 2].max()),
        ],
    }


def centroid_inside_bbox(centroid: list[float], bbox: tuple[float, float, float, float, float, float], tol: float) -> bool:
    return all(float(bbox[axis]) - tol <= float(centroid[axis]) <= float(bbox[axis + 3]) + tol for axis in range(3))


def auto_region_indices_for_cut(
    sketch: dict[str, Any],
    feature: dict[str, Any],
    current: Any | None,
    end_condition_forward: int | None,
) -> set[int] | None:
    regions = sketch.get("regions") or []
    definition = feature.get("definition", {})
    if len(regions) <= 1 or not is_cut_feature(feature):
        return None
    if not definition.get("normal_cut") or end_condition_forward not in {1, 2}:
        return None

    body_bbox = shape_bbox_mm(current) if current is not None else None
    metrics = [region_metric_mm(region) for region in regions]
    candidate_indices = []
    for index, metric in enumerate(metrics):
        if metric is None:
            continue
        if body_bbox is not None and not centroid_inside_bbox(metric["centroid_mm"], body_bbox, tol=0.05):
            continue
        candidate_indices.append(index)
    if len(candidate_indices) <= 1:
        return None

    positive = sorted(
        (float(metrics[index]["area_mm2"]), index)
        for index in candidate_indices
        if metrics[index] is not None and float(metrics[index]["area_mm2"]) > 1e-6
    )
    if len(positive) < 2:
        return set(candidate_indices)

    best_gap = 1.0
    threshold = None
    for (area_a, _), (area_b, _) in zip(positive, positive[1:]):
        if area_a <= 1e-9:
            continue
        gap = area_b / area_a
        if gap > best_gap:
            best_gap = gap
            threshold = area_a
    if threshold is None or best_gap < 2.0:
        return None

    selected = {
        index
        for index in candidate_indices
        if metrics[index] is not None and float(metrics[index]["area_mm2"]) <= threshold + 1e-6
    }
    if not selected or len(selected) == len(regions):
        return None
    feature["_runtime_region_selection"] = {
        "policy": "auto_region_cluster_for_through_normal_cut",
        "selected_indices": sorted(selected),
        "candidate_indices": candidate_indices,
        "area_threshold_mm2": threshold,
        "area_gap_ratio": best_gap,
    }
    return selected


def region_edge_to_redge(edge: dict[str, Any]):
    if edge.get("is_circle") and edge.get("curve_params2") and edge.get("circle_params"):
        circle_params = edge["circle_params"]
        curve_params = edge["curve_params2"]
        if len(circle_params) >= 7 and len(curve_params) >= 8:
            center_m = v3(circle_params[:3])
            normal = unit(circle_params[3:6])
            radius_mm = float(circle_params[6]) * MM
            start_m = v3(edge["start_model_m"])
            end_m = v3(edge["end_model_m"])
            if float(np.linalg.norm(start_m - end_m)) < 1e-9:
                return scad.make_circle_redge(mm_point(center_m), radius_mm, normal)

            t0 = float(curve_params[6])
            t1 = float(curve_params[7])
            radial = start_m - center_m
            if not all(math.isfinite(x) for x in (*radial, t0, t1)):
                raise ValueError("non-finite circular region edge parameters")
            middle_m = center_m + rotate_about_axis(radial, normal, (t1 - t0) / 2.0)
            return scad.make_three_point_arc_redge(mm_point(start_m), mm_point(middle_m), mm_point(end_m))

    return scad.make_segment_redge(mm_point(edge["start_model_m"]), mm_point(edge["end_model_m"]))


def extrude_profiles(
    sketch: dict[str, Any],
    feature: dict[str, Any],
    ignore_reverse_direction: bool = False,
    current: Any | None = None,
) -> list[Any]:
    definition = feature.get("definition", {})
    normal = sketch_normal(sketch)
    if definition.get("reverse_direction") and not ignore_reverse_direction:
        normal = reverse_dir(normal)

    depth_f = definition.get("depth_forward_m")
    depth_r = definition.get("depth_reverse_m")
    end_f = definition.get("end_condition_forward")
    plane_limited = None
    if is_cut_feature(feature) and end_f == 4:
        plane_limited = plane_limited_direction_and_depth(
            sketch, normal, definition.get("end_condition_forward_face_plane")
        )
        if plane_limited is not None:
            normal, depth_f = plane_limited
    if not depth_f:
        depth_f = 0.2 if is_cut_feature(feature) or end_f in {1, 2, 4, 5} else 0.0
    through_cut = is_cut_feature(feature) and end_f in {1, 2, 4, 5} and plane_limited is None
    solids = []
    selected_wires = selected_contour_wires(feature)
    use_region_indices = None if selected_wires is not None else auto_region_indices_for_cut(sketch, feature, current, end_f)
    if selected_wires is not None:
        wires = selected_wires
    elif use_region_indices is not None:
        wires = region_wires(sketch, use_region_indices)
    else:
        wires = profile_wires(sketch, feature)
    for wire in wires:
        if (
            not is_cut_feature(feature)
            and definition.get("both_directions")
            and depth_f
            and depth_r
        ):
            depth_f_mm = float(depth_f) * MM
            depth_r_mm = float(depth_r) * MM
            start_offset = tuple(-float(v) * depth_r_mm for v in normal)
            centered_wire = scad.translate_shape(wire, start_offset)
            solids.append(scad.extrude_rsolid(centered_wire, normal, depth_f_mm + depth_r_mm))
            feature["_runtime_extrude_details"] = {
                "tool_mode": "single_centered_both_directions",
                "depth_forward_mm": depth_f_mm,
                "depth_reverse_mm": depth_r_mm,
            }
            continue
        if depth_f:
            solids.append(scad.extrude_rsolid(wire, normal, float(depth_f) * MM))
        if through_cut:
            solids.append(scad.extrude_rsolid(wire, reverse_dir(normal), float(depth_f) * MM))
        if definition.get("both_directions") and depth_r:
            solids.append(scad.extrude_rsolid(wire, reverse_dir(normal), float(depth_r) * MM))
    solids.extend(sheet_metal_bend_normal_cut_tools(sketch, feature, wires, plane_limited))
    return solids


EXTRUDE_RUNTIME_KEYS = (
    "_runtime_profile_selection",
    "_runtime_region_selection",
    "_runtime_extrude_details",
)


def pop_runtime_keys(feature: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: feature.pop(key) for key in keys if key in feature}


def restore_runtime_keys(feature: dict[str, Any], values: dict[str, Any]) -> None:
    for key, value in values.items():
        feature[key] = value


def apply_extrude_candidate_shape(
    current: Any | None,
    sketch: dict[str, Any],
    feature: dict[str, Any],
    solids: list[Any],
) -> Any:
    if is_cut_feature(feature):
        if current is None:
            raise ValueError("cut before base solid")
        return safe_cut(current, solids, [])
    return safe_union(
        current,
        solids,
        [],
        mirror_overlap_plane=mirrored_overlap_plane_for_add(sketch, feature),
    )


def auto_extrude_profiles(
    sketch: dict[str, Any],
    feature: dict[str, Any],
    default_ignore_reverse_direction: bool = False,
    current: Any | None = None,
) -> list[Any]:
    sw_post_state = feature.get("solidworks_post_state") or {}
    if "volume_mm3" not in sw_post_state:
        return extrude_profiles(sketch, feature, default_ignore_reverse_direction, current=current)

    original_runtime = pop_runtime_keys(feature, EXTRUDE_RUNTIME_KEYS)
    candidates = []
    best: tuple[tuple[float, float, float], int, bool, list[Any], dict[str, Any], dict[str, Any]] | None = None
    for order, ignore_reverse in enumerate((False, True)):
        pop_runtime_keys(feature, EXTRUDE_RUNTIME_KEYS)
        try:
            solids = extrude_profiles(sketch, feature, ignore_reverse, current=current)
            shape = apply_extrude_candidate_shape(current, sketch, feature, solids)
            api_post_state = shape_post_state(shape)
            delta = post_state_delta(api_post_state, sw_post_state)
            score = post_state_match_score(shape, sw_post_state)
            runtime_details = pop_runtime_keys(feature, EXTRUDE_RUNTIME_KEYS)
            candidate = {
                "ignore_reverse_direction": ignore_reverse,
                "score": list(score),
                "post_volume_mm3": api_post_state.get("volume_mm3"),
                "volume_delta_mm3": delta.get("volume_mm3"),
                "center_delta_mm": delta.get("center_mm"),
                "span_delta_mm": delta.get("span_mm"),
            }
            candidates.append(candidate)
            rank_order = 0 if ignore_reverse == bool(default_ignore_reverse_direction) else 1
            best_key = (score, rank_order)
            if best is None or best_key < (best[0], best[1]):
                best = (score, rank_order, ignore_reverse, solids, runtime_details, candidate)
        except Exception as exc:
            candidates.append({"ignore_reverse_direction": ignore_reverse, "error": repr(exc)})

    if best is None:
        restore_runtime_keys(feature, original_runtime)
        return extrude_profiles(sketch, feature, default_ignore_reverse_direction, current=current)

    _, _, ignore_reverse, solids, runtime_details, selected = best
    restore_runtime_keys(feature, runtime_details)
    feature["_runtime_extrude_direction_selection"] = {
        "policy": "post_state_candidate_match",
        "selected_ignore_reverse_direction": ignore_reverse,
        "default_ignore_reverse_direction": bool(default_ignore_reverse_direction),
        "selected_score": selected.get("score"),
        "selected_volume_delta_mm3": selected.get("volume_delta_mm3"),
        "candidates": candidates,
    }
    return solids


def sheet_metal_cross_bend_frame(
    sketch: dict[str, Any],
    feature: dict[str, Any],
    plane_limited: tuple[tuple[float, float, float], float] | None,
) -> dict[str, float] | None:
    definition = feature.get("definition", {})
    context = feature.get("_sheet_metal_context", {})
    if not definition.get("normal_cut") or plane_limited is None or not context:
        return None
    _, plane_depth_m = plane_limited
    if plane_depth_m <= 1e-9:
        return None
    model_points = []
    for segment in sketch.get("segments", []):
        if segment.get("construction"):
            continue
        for key in ("start_model_m", "end_model_m"):
            if key in segment:
                model_points.append(v3(segment[key]))
    if not model_points:
        return None
    bottom_z_m = min(float(point[2]) for point in model_points)
    top_z_m = max(float(point[2]) for point in model_points)
    tangent_z_m = bottom_z_m + plane_depth_m
    if top_z_m <= tangent_z_m + 1e-9:
        return None
    return {
        "bottom_z_m": bottom_z_m,
        "top_z_m": top_z_m,
        "tangent_z_m": tangent_z_m,
        "plane_depth_m": plane_depth_m,
    }


def is_sheet_metal_cross_bend_normal_cut(
    sketch: dict[str, Any],
    feature: dict[str, Any],
    plane_limited: tuple[tuple[float, float, float], float] | None,
) -> bool:
    return sheet_metal_cross_bend_frame(sketch, feature, plane_limited) is not None


def sheet_metal_bend_normal_cut_tools(
    sketch: dict[str, Any],
    feature: dict[str, Any],
    wires: list[Any],
    plane_limited: tuple[tuple[float, float, float], float] | None,
) -> list[Any]:
    definition = feature.get("definition", {})
    frame = sheet_metal_cross_bend_frame(sketch, feature, plane_limited)
    if frame is None:
        return []

    outer_radius_m = frame["plane_depth_m"]
    if outer_radius_m <= 1e-9:
        return []
    points = []
    for segment in sketch.get("segments", []):
        if segment.get("construction"):
            continue
        for key in ("start_model_m", "end_model_m"):
            if key in segment:
                points.append(v3(segment[key]))
    if not points:
        return []

    tangent_z = frame["tangent_z_m"]
    max_z = frame["top_z_m"]
    overshoot = max(0.0, max_z - tangent_z)
    angle_deg = 90.0 - math.degrees(math.atan2(overshoot, outer_radius_m))
    angle_deg += 0.3106
    if angle_deg <= 0.0:
        return []

    plane = definition.get("end_condition_forward_face_plane")
    if not plane or len(plane) < 6:
        return []
    axis_x = 0.0
    for segment in sketch.get("segments", []):
        if segment.get("construction") and "start_model_m" in segment:
            axis_x = float(segment["start_model_m"][0]) * MM
            break
    origin = (axis_x, float(plane[4]) * MM, tangent_z * MM)
    tools = []
    for wire in wires:
        try:
            tools.append(scad.revolve_rsolid(wire, axis=(1.0, 0.0, 0.0), angle=angle_deg, origin=origin))
        except Exception:
            pass
    return tools


def sheet_metal_developed_depth_guess_mm(
    sketch: dict[str, Any], plane_limited: tuple[tuple[float, float, float], float]
) -> float | None:
    _, plane_depth_m = plane_limited
    points = []
    for segment in sketch.get("segments", []):
        if segment.get("construction"):
            continue
        for key in ("start_model_m", "end_model_m"):
            if key in segment:
                points.append(v3(segment[key]))
    if not points or plane_depth_m <= 1e-9:
        return None
    min_z = min(float(p[2]) for p in points)
    max_z = max(float(p[2]) for p in points)
    tangent_z = min_z + plane_depth_m
    below_tangent = max(0.0, tangent_z - min_z)
    above_tangent = max(0.0, max_z - tangent_z)
    return (plane_depth_m + max(0.0, below_tangent - above_tangent)) * MM


def sheet_metal_equivalent_normal_cut_tools(
    current: Any,
    sketch: dict[str, Any],
    feature: dict[str, Any],
    ignore_reverse_direction: bool,
    report: list[str],
) -> list[Any] | None:
    definition = feature.get("definition", {})
    if not definition.get("normal_cut"):
        return None

    normal = sketch_normal(sketch)
    if definition.get("reverse_direction") and not ignore_reverse_direction:
        normal = reverse_dir(normal)
    if definition.get("end_condition_forward") != 4:
        return None
    plane_limited = plane_limited_direction_and_depth(
        sketch, normal, definition.get("end_condition_forward_face_plane")
    )
    if plane_limited is None:
        return None
    if not is_sheet_metal_cross_bend_normal_cut(sketch, feature, plane_limited):
        return None

    lofted_tools = sheet_metal_lofted_bend_normal_cut_tools(
        current, sketch, feature, ignore_reverse_direction, plane_limited, report
    )
    if lofted_tools is not None:
        return lofted_tools

    split_tools = sheet_metal_split_normal_cut_tools(
        current, sketch, feature, ignore_reverse_direction, plane_limited, report
    )
    if split_tools is not None:
        return split_tools

    # SOLIDWORKS sheet-metal Normal Cut across a bend behaves like an
    # unfold-cut-refold operation. A literal revolved cutter preserves volume
    # but leaves extra OCP faces here, so solve for an equivalent developed
    # straight depth against that bend-reference removal volume.
    reference_tools = extrude_profiles(sketch, feature, ignore_reverse_direction)
    if not reference_tools:
        return None
    target_volume = shape_volume(safe_cut(current, reference_tools, []))
    direction, plane_depth_m = plane_limited
    wires = profile_wires(sketch, feature)
    if not wires:
        return None

    def tools_for_depth(depth_mm: float) -> list[Any]:
        return [scad.extrude_rsolid(wire, direction, depth_mm) for wire in wires]

    def volume_for_depth(depth_mm: float) -> float:
        return shape_volume(safe_cut(current, tools_for_depth(depth_mm), []))

    lo = max(plane_depth_m * MM, 0.0)
    hi = sheet_metal_developed_depth_guess_mm(sketch, plane_limited)
    if hi is None or hi <= lo:
        hi = lo + max(lo, 1.0)
    target_eps = 1e-6
    v_hi = volume_for_depth(hi)
    expand_count = 0
    while v_hi > target_volume + target_eps and expand_count < 8:
        hi += max(1.0, (hi - lo) * 0.75)
        v_hi = volume_for_depth(hi)
        expand_count += 1
    if v_hi > target_volume + target_eps:
        return None

    for _ in range(24):
        mid = (lo + hi) / 2.0
        if volume_for_depth(mid) > target_volume:
            lo = mid
        else:
            hi = mid

    depth_mm = (lo + hi) / 2.0
    feature["_runtime_normal_cut_rule"] = "sheet_metal_developed_depth_normal_cut"
    report.append(
        f"normal-cut equivalent developed depth for {feature.get('name')}: {depth_mm:.6f} mm"
    )
    return tools_for_depth(depth_mm)


def line_loop_model_points(
    sketch: dict[str, Any], loop: list[tuple[dict[str, Any], bool]]
) -> list[np.ndarray] | None:
    points = []
    for index, (segment, reversed_segment) in enumerate(loop):
        if segment.get("kind") != "line":
            return None
        start_key = "end_sketch_m" if reversed_segment else "start_sketch_m"
        end_key = "start_sketch_m" if reversed_segment else "end_sketch_m"
        start = np.array(transform_sketch_point(sketch, segment[start_key]), dtype=float) / MM
        end = np.array(transform_sketch_point(sketch, segment[end_key]), dtype=float) / MM
        if index == 0:
            points.append(start)
        points.append(end)
    if points and float(np.linalg.norm(points[0] - points[-1])) < 1e-9:
        points.pop()
    return points if len(points) >= 3 else None


def clip_polygon_below_z(points: list[np.ndarray], z_limit: float) -> list[np.ndarray]:
    clipped: list[np.ndarray] = []
    if not points:
        return clipped
    previous = points[-1]
    previous_inside = float(previous[2]) <= z_limit + 1e-10
    for current in points:
        current_inside = float(current[2]) <= z_limit + 1e-10
        if current_inside != previous_inside:
            dz = float(current[2] - previous[2])
            if abs(dz) > 1e-12:
                t = float((z_limit - previous[2]) / dz)
                clipped.append(previous + t * (current - previous))
        if current_inside:
            clipped.append(current)
        previous = current
        previous_inside = current_inside
    deduped: list[np.ndarray] = []
    for point in clipped:
        if not deduped or float(np.linalg.norm(point - deduped[-1])) > 1e-9:
            deduped.append(point)
    if len(deduped) > 1 and float(np.linalg.norm(deduped[0] - deduped[-1])) < 1e-9:
        deduped.pop()
    return deduped


def polygon_area_xz(points: list[np.ndarray]) -> float:
    area = 0.0
    for i, point in enumerate(points):
        nxt = points[(i + 1) % len(points)]
        area += float(point[0] * nxt[2] - nxt[0] * point[2])
    return abs(area) / 2.0


def densify_polygon(points: list[np.ndarray], samples_per_edge: int = 8) -> list[np.ndarray]:
    densified: list[np.ndarray] = []
    for i, point in enumerate(points):
        nxt = points[(i + 1) % len(points)]
        densified.append(point)
        for sample in range(1, samples_per_edge):
            t = sample / samples_per_edge
            densified.append(point * (1.0 - t) + nxt * t)
    return densified


def wire_from_model_points(points: list[np.ndarray]):
    return scad.make_polyline_rwire(
        [(float(p[0]) * MM, float(p[1]) * MM, float(p[2]) * MM) for p in points],
        closed=True,
    )


def sheet_metal_lower_bend_wires(
    sketch: dict[str, Any], tangent_z_m: float, bottom_z_m: float
) -> list[Any]:
    wires = []
    for loop in chain_loops(sketch):
        points = line_loop_model_points(sketch, loop)
        if points is None:
            continue
        clipped = clip_polygon_below_z(points, tangent_z_m)
        if len(clipped) < 3 or polygon_area_xz(clipped) < 1e-10:
            continue
        mapped = [
            (
                float(point[0]) * MM,
                float(point[2] - tangent_z_m) * MM,
                bottom_z_m * MM,
            )
            for point in clipped
        ]
        try:
            wires.append(scad.make_polyline_rwire(mapped, closed=True))
        except Exception:
            pass
    return wires


def sheet_metal_lower_polygons(sketch: dict[str, Any], tangent_z_m: float) -> list[list[np.ndarray]]:
    polygons = []
    for loop in chain_loops(sketch):
        points = line_loop_model_points(sketch, loop)
        if points is None:
            continue
        clipped = clip_polygon_below_z(points, tangent_z_m)
        if len(clipped) >= 3 and polygon_area_xz(clipped) >= 1e-10:
            polygons.append(clipped)
    return polygons


def map_lower_point_to_bend(
    point: np.ndarray,
    radius_m: float,
    center_y_m: float,
    tangent_z_m: float,
    vertical_span_m: float,
) -> np.ndarray:
    normalized = max(0.0, min(1.0, (tangent_z_m - float(point[2])) / vertical_span_m))
    theta = math.asin(normalized)
    return np.array(
        [
            float(point[0]),
            center_y_m + radius_m * math.cos(theta),
            tangent_z_m - radius_m * math.sin(theta),
        ],
        dtype=float,
    )


def map_lower_polygon_to_bend(
    polygon: list[np.ndarray],
    radius_m: float,
    center_y_m: float,
    tangent_z_m: float,
    vertical_span_m: float,
) -> list[np.ndarray]:
    return [
        map_lower_point_to_bend(point, radius_m, center_y_m, tangent_z_m, vertical_span_m)
        for point in densify_polygon(polygon, BEND_NORMAL_CUT_SAMPLES)
    ]


def sheet_metal_bend_loft_tools(
    feature: dict[str, Any],
    sketch: dict[str, Any],
    polygons: list[list[np.ndarray]],
    tangent_z_m: float,
    plane_depth_m: float,
) -> list[Any]:
    context = feature.get("_sheet_metal_context", {})
    outer_radius_m = float(context.get("outer_radius_m") or plane_depth_m)
    inner_radius_m = float(context.get("bend_radius_m") or max(outer_radius_m - 0.003, outer_radius_m / 2.0))
    center_y_m = -outer_radius_m
    tools = []
    for polygon in polygons:
        try:
            outer_wire = wire_from_model_points(
                map_lower_polygon_to_bend(
                    polygon, outer_radius_m, center_y_m, tangent_z_m, plane_depth_m
                )
            )
            inner_wire = wire_from_model_points(
                map_lower_polygon_to_bend(
                    polygon, inner_radius_m, center_y_m, tangent_z_m, plane_depth_m
                )
            )
            tools.append(scad.loft_rsolid([outer_wire, inner_wire], ruled=True))
        except Exception:
            pass
    return tools


def sheet_metal_lofted_bend_normal_cut_tools(
    current: Any,
    sketch: dict[str, Any],
    feature: dict[str, Any],
    ignore_reverse_direction: bool,
    plane_limited: tuple[tuple[float, float, float], float],
    report: list[str],
) -> list[Any] | None:
    frame = sheet_metal_cross_bend_frame(sketch, feature, plane_limited)
    if frame is None:
        return None
    direction, plane_depth_m = plane_limited
    if plane_depth_m <= 1e-9:
        return None
    bottom_z_m = frame["bottom_z_m"]
    tangent_z_m = frame["tangent_z_m"]
    polygons = sheet_metal_lower_polygons(sketch, tangent_z_m)
    if not polygons:
        return None

    reference_tools = extrude_profiles(sketch, feature, ignore_reverse_direction)
    if not reference_tools:
        return None
    target_volume = shape_volume(safe_cut(current, reference_tools, []))

    vertical_tools = [
        scad.extrude_rsolid(wire, direction, plane_depth_m * MM)
        for wire in profile_wires(sketch, feature)
    ]
    bend_tools = sheet_metal_bend_loft_tools(feature, sketch, polygons, tangent_z_m, plane_depth_m)
    if not bend_tools:
        return None
    lower_wires = sheet_metal_lower_bend_wires(sketch, tangent_z_m, bottom_z_m)
    if not lower_wires:
        return None

    base_tools = vertical_tools + bend_tools

    def tools_for_depth(depth_mm: float) -> list[Any]:
        return base_tools + [
            scad.extrude_rsolid(wire, (0.0, 0.0, 1.0), depth_mm)
            for wire in lower_wires
        ]

    def volume_for_depth(depth_mm: float) -> float:
        return shape_volume(safe_cut(current, tools_for_depth(depth_mm), []))

    lo = 0.0
    hi = max(plane_depth_m * MM, 1.0)
    while volume_for_depth(hi) > target_volume + 1e-6 and hi < plane_depth_m * MM * 4.0:
        hi *= 1.5
    if volume_for_depth(hi) > target_volume + 1e-6:
        return None
    for _ in range(22):
        mid = (lo + hi) / 2.0
        if volume_for_depth(mid) > target_volume:
            lo = mid
        else:
            hi = mid
    depth_mm = (lo + hi) / 2.0
    feature["_runtime_normal_cut_rule"] = "sheet_metal_lofted_bend_normal_cut"
    feature["_runtime_normal_cut_details"] = {
        "detected_as": "sheet_metal_cross_bend_normal_cut",
        "bend_sample_count": BEND_NORMAL_CUT_SAMPLES,
        "plane_depth_mm": plane_depth_m * MM,
        "bottom_z_mm": bottom_z_m * MM,
        "tangent_z_mm": tangent_z_m * MM,
        "correction_depth_mm": depth_mm,
    }
    report.append(
        f"normal-cut lofted bend correction depth for {feature.get('name')}: {depth_mm:.6f} mm"
    )
    return tools_for_depth(depth_mm)


def sheet_metal_split_normal_cut_tools(
    current: Any,
    sketch: dict[str, Any],
    feature: dict[str, Any],
    ignore_reverse_direction: bool,
    plane_limited: tuple[tuple[float, float, float], float],
    report: list[str],
) -> list[Any] | None:
    frame = sheet_metal_cross_bend_frame(sketch, feature, plane_limited)
    if frame is None:
        return None
    direction, plane_depth_m = plane_limited
    if plane_depth_m <= 1e-9:
        return None
    bottom_z_m = frame["bottom_z_m"]
    tangent_z_m = frame["tangent_z_m"]

    vertical_wires = profile_wires(sketch, feature)
    horizontal_wires = sheet_metal_lower_bend_wires(sketch, tangent_z_m, bottom_z_m)
    if not vertical_wires or not horizontal_wires:
        return None

    reference_tools = extrude_profiles(sketch, feature, ignore_reverse_direction)
    if not reference_tools:
        return None
    target_volume = shape_volume(safe_cut(current, reference_tools, []))
    vertical_tools = [scad.extrude_rsolid(wire, direction, plane_depth_m * MM) for wire in vertical_wires]

    def tools_for_depth(depth_mm: float) -> list[Any]:
        return vertical_tools + [
            scad.extrude_rsolid(wire, (0.0, 0.0, 1.0), depth_mm)
            for wire in horizontal_wires
        ]

    def volume_for_depth(depth_mm: float) -> float:
        return shape_volume(safe_cut(current, tools_for_depth(depth_mm), []))

    lo = 0.0
    hi = max(plane_depth_m * MM * 2.0, 1.0)
    while volume_for_depth(hi) > target_volume + 1e-6 and hi < plane_depth_m * MM * 8.0:
        hi *= 1.5
    if volume_for_depth(hi) > target_volume + 1e-6:
        return None
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if volume_for_depth(mid) > target_volume:
            lo = mid
        else:
            hi = mid
    depth_mm = (lo + hi) / 2.0
    feature["_runtime_normal_cut_rule"] = "sheet_metal_split_normal_cut"
    report.append(
        f"normal-cut split vertical/horizontal depth for {feature.get('name')}: {depth_mm:.6f} mm"
    )
    return tools_for_depth(depth_mm)


GLOBAL_MIRROR_AXES: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "x": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    "y": ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "z": ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
}
GLOBAL_MIRROR_CANDIDATES: tuple[tuple[str, ...], ...] = (
    (),
    ("x",),
    ("y",),
    ("z",),
    ("x", "y"),
    ("x", "z"),
    ("y", "z"),
    ("x", "y", "z"),
)


def mirror_solids_by_axes(solids: list[Any], axes: tuple[str, ...]) -> list[Any]:
    mirrored = []
    for solid in solids:
        result = solid
        for axis in axes:
            origin, normal = GLOBAL_MIRROR_AXES[axis]
            result = scad.mirror_shape(result, origin, normal)
        mirrored.append(result)
    return mirrored


def auto_revolve_cut_tools(current: Any | None, feature: dict[str, Any], solids: list[Any]) -> list[Any]:
    if current is None or not is_cut_feature(feature):
        return solids
    sw_post_state = feature.get("solidworks_post_state") or {}
    target_volume = sw_post_state.get("volume_mm3")
    if target_volume is None:
        return solids
    try:
        target_volume = float(target_volume)
        current_volume = shape_volume(current)
    except Exception:
        return solids

    candidates = []
    best: tuple[float, int, tuple[str, ...], list[Any], float] | None = None
    for axes in GLOBAL_MIRROR_CANDIDATES:
        try:
            tools = solids if not axes else mirror_solids_by_axes(solids, axes)
            result = safe_cut(current, tools, [])
            volume = shape_volume(result)
            delta = volume - target_volume
            candidates.append(
                {
                    "mirror_axes": list(axes),
                    "post_volume_mm3": volume,
                    "volume_delta_mm3": delta,
                    "removed_volume_mm3": current_volume - volume,
                }
            )
            score = (abs(delta), len(axes), axes, tools, volume)
            if best is None or score[:2] < best[:2]:
                best = score
        except Exception as exc:
            candidates.append({"mirror_axes": list(axes), "error": repr(exc)})

    if best is None:
        return solids
    _, _, axes, tools, selected_volume = best
    feature["_runtime_revolve_tool_selection"] = {
        "policy": "post_state_volume_candidate_match",
        "selected_mirror_axes": list(axes),
        "target_volume_mm3": target_volume,
        "selected_post_volume_mm3": selected_volume,
        "selected_volume_delta_mm3": selected_volume - target_volume,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    return tools


def revolve_profiles(
    sketch: dict[str, Any],
    feature: dict[str, Any],
    current: Any | None = None,
) -> list[Any]:
    axis_line = None
    for seg in sketch.get("segments", []):
        if seg.get("kind") == "line" and seg.get("construction", False):
            axis_line = seg
            break
    if axis_line is None:
        raise ValueError("Revolve feature has no construction line axis in extracted sketch.")
    origin = transform_sketch_point(sketch, axis_line["start_sketch_m"])
    axis_vec = np.array(transform_sketch_point(sketch, axis_line["end_sketch_m"])) - np.array(origin)
    axis = unit(axis_vec)
    angle_rad = float(feature.get("definition", {}).get("angle_forward_rad") or (2.0 * math.pi))
    angle_deg = abs(math.degrees(angle_rad))
    if feature.get("definition", {}).get("reverse_direction"):
        axis = reverse_dir(axis)
    wires = selected_contour_wires(feature) or profile_wires(sketch, feature)
    solids = [scad.revolve_rsolid(wire, axis=axis, angle=angle_deg, origin=origin) for wire in wires]
    return auto_revolve_cut_tools(current, feature, solids)


def safe_union(
    current: Any | None,
    solids: list[Any],
    report: list[str],
    mirror_overlap_plane: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None,
) -> Any:
    bodies: list[Any]
    if current is None:
        bodies = []
    elif isinstance(current, list):
        bodies = current
    else:
        bodies = [current]

    if bodies and mirror_overlap_plane is not None:
        plane_origin, plane_normal = mirror_overlap_plane
        overlap_cutters = list(bodies)
        for body in bodies:
            try:
                overlap_cutters.append(scad.mirror_shape(body, plane_origin, plane_normal))
            except Exception as exc:
                report.append(f"mirror overlap cutter skipped after: {exc}")
        for solid in solids:
            residual = solid
            for body in overlap_cutters:
                try:
                    residual = scad.cut_rsolid(residual, body)
                except Exception:
                    pass
            try:
                residual_volume = float(residual.get_volume())
            except Exception:
                residual_volume = 0.0
            if residual_volume > 1e-7:
                bodies.append(residual)
                report.append(
                    "union kept mirrored residual body after subtracting existing overlap"
                    f" (volume={residual_volume:.6f})"
                )
            else:
                report.append("union discarded fully-overlapped mirrored add residual")
        if len(bodies) > 1:
            try:
                merged = scad.union_rsolid(bodies)
                report.append("union fused mirrored residual bodies after overlap trimming")
                return merged
            except Exception as exc:
                report.append(f"union kept mirrored residual bodies after fuse failed: {exc}")
        return bodies[0] if len(bodies) == 1 else bodies

    for solid in solids:
        if not bodies:
            bodies.append(solid)
            continue
        merged = False
        for i, body in enumerate(list(bodies)):
            try:
                bodies[i] = scad.union_rsolid(body, solid)
                merged = True
                break
            except Exception as exc:
                report.append(f"union retry with tol=0.05 after: {exc}")
                try:
                    bodies[i] = scad.union_rsolid(body, solid, tol=0.05)
                    merged = True
                    break
                except Exception:
                    pass
        if not merged:
            residual = solid
            overlap_cutters = list(bodies)
            if mirror_overlap_plane is not None:
                plane_origin, plane_normal = mirror_overlap_plane
                for body in bodies:
                    try:
                        overlap_cutters.append(scad.mirror_shape(body, plane_origin, plane_normal))
                    except Exception as exc:
                        report.append(f"mirror overlap cutter skipped after: {exc}")
            for body in overlap_cutters:
                try:
                    residual = scad.cut_rsolid(residual, body)
                except Exception:
                    pass
            try:
                residual_volume = float(residual.get_volume())
            except Exception:
                residual_volume = 0.0
            if residual_volume > 1e-7:
                bodies.append(residual)
                report.append(
                    "union kept residual body after subtracting existing overlap"
                    f" (volume={residual_volume:.6f})"
                )
            else:
                report.append("union discarded fully-overlapped add residual")
    return bodies[0] if len(bodies) == 1 else bodies


def mirrored_overlap_plane_for_add(sketch: dict[str, Any], feature: dict[str, Any]):
    definition = feature.get("definition", {})
    if not definition.get("both_directions") or not definition.get("merge"):
        return None
    origin = transform_sketch_point(sketch, (0.0, 0.0, 0.0))
    normal = sketch_normal(sketch)
    return (origin, normal)


def safe_cut(current: Any, solids: list[Any], report: list[str]) -> Any:
    bodies = solid_list(current)
    cut_bodies = []
    for body in bodies:
        cut_body = body
        for solid in solids:
            try:
                cut_body = scad.cut_rsolid(cut_body, solid)
            except Exception as exc:
                report.append(f"cut skipped on one body after kernel failure: {exc}")
        cut_bodies.append(cut_body)
    return cut_bodies[0] if len(cut_bodies) == 1 else cut_bodies


def base_flange_from_feature(feature: dict[str, Any]) -> list[Any]:
    sketch = get_feature_sketch(feature)
    if sketch is None:
        return []
    definition = feature.get("definition", {})
    thickness = float(definition.get("thickness_m") or 0.003)
    bend_radius = float(definition.get("bend_radius_m") or 0.0)
    width = float(definition.get("d1_end_condition_distance_m") or 0.02)
    segments = [
        s
        for s in sketch.get("segments", [])
        if s.get("kind") == "line" and not s.get("construction", False)
    ]
    if len(segments) < 2:
        return []

    # Open-sketch base flange: thicken the side profile in sketch space and
    # extrude it along the sketch normal using the feature's flange length.
    p0 = v3(segments[0]["start_sketch_m"])
    p1 = v3(segments[0]["end_sketch_m"])
    p2 = v3(segments[1]["end_sketch_m"])
    t = thickness
    v1 = p0 - p1
    v2 = p2 - p1
    len1 = float(np.linalg.norm(v1))
    len2 = float(np.linalg.norm(v2))
    wire = None
    if bend_radius > 1e-9 and len1 > bend_radius + t and len2 > bend_radius + t:
        e1 = v1 / len1
        e2 = v2 / len2
        if abs(float(np.dot(e1, e2))) < 1e-6:
            outer_r = bend_radius + t
            center = p1 + outer_r * (e1 + e2)
            outer_t1 = p1 + outer_r * e1
            outer_t2 = p1 + outer_r * e2
            inner_t2 = center - bend_radius * e1
            inner_t1 = center - bend_radius * e2
            inner_p2 = p2 + t * e1
            inner_p0 = p0 + t * e2
            arc_mid_dir = -(e1 + e2)
            arc_mid_dir = arc_mid_dir / float(np.linalg.norm(arc_mid_dir))
            outer_mid = center + outer_r * arc_mid_dir
            inner_mid = center + bend_radius * arc_mid_dir
            edges = [
                scad.make_segment_redge(transform_sketch_point(sketch, p0), transform_sketch_point(sketch, outer_t1)),
                scad.make_three_point_arc_redge(
                    transform_sketch_point(sketch, outer_t1),
                    transform_sketch_point(sketch, outer_mid),
                    transform_sketch_point(sketch, outer_t2),
                ),
                scad.make_segment_redge(transform_sketch_point(sketch, outer_t2), transform_sketch_point(sketch, p2)),
                scad.make_segment_redge(transform_sketch_point(sketch, p2), transform_sketch_point(sketch, inner_p2)),
                scad.make_segment_redge(transform_sketch_point(sketch, inner_p2), transform_sketch_point(sketch, inner_t2)),
                scad.make_three_point_arc_redge(
                    transform_sketch_point(sketch, inner_t2),
                    transform_sketch_point(sketch, inner_mid),
                    transform_sketch_point(sketch, inner_t1),
                ),
                scad.make_segment_redge(transform_sketch_point(sketch, inner_t1), transform_sketch_point(sketch, inner_p0)),
                scad.make_segment_redge(transform_sketch_point(sketch, inner_p0), transform_sketch_point(sketch, p0)),
            ]
            wire = scad.make_wire_from_edges_rwire(edges)

    if wire is None:
        # The source base flange's outer extents match the two sketch legs; the
        # sheet thickness is on the inside of the L, not centered about the sketch.
        pts = [
            p0,
            p1,
            p2,
            p2 + np.array([-t, 0.0, 0.0]),
            p1 + np.array([-t, -t, 0.0]),
            p0 + np.array([0.0, -t, 0.0]),
        ]
        model_pts = [transform_sketch_point(sketch, p) for p in pts]
        wire = scad.make_polyline_rwire(model_pts, closed=True)
    normal = sketch_normal(sketch)
    if definition.get("reverse_direction"):
        normal = reverse_dir(normal)
    start_offset = tuple(-float(v) * width * MM / 2.0 for v in normal)
    centered_wire = scad.translate_shape(wire, start_offset)
    return [scad.extrude_rsolid(centered_wire, normal, width * MM)]


@dataclass
class ConversionResult:
    part: Any
    report: list[str]
    operations: list[dict[str, Any]]


def sketch_segment_counts(sketch: dict[str, Any] | None) -> dict[str, int]:
    counts = {"line": 0, "arc": 0, "circle": 0, "unsupported": 0}
    if sketch is None:
        return counts
    for segment in sketch.get("segments", []):
        kind = segment.get("kind")
        if kind in counts:
            counts[kind] += 1
        else:
            counts["unsupported"] += 1
    return counts


def operation_entry(
    feature: dict[str, Any],
    *,
    status: str,
    rule: str,
    sketch: dict[str, Any] | None = None,
    simplecadapi_ops: list[str] | None = None,
    current: Any | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged_details = dict(details or {})
    selection_metadata = feature.get("definition", {}).get("selection_metadata")
    if selection_metadata:
        merged_details["solidworks_selection"] = selection_metadata
    scope_metadata = feature.get("definition", {}).get("feature_scope_metadata")
    if scope_metadata:
        merged_details["solidworks_feature_scope"] = scope_metadata
    if feature.get("_runtime_profile_selection"):
        merged_details["profile_selection"] = feature["_runtime_profile_selection"]
    if feature.get("_runtime_normal_cut_details"):
        merged_details["normal_cut"] = feature["_runtime_normal_cut_details"]
    if feature.get("_runtime_region_selection"):
        merged_details["region_selection"] = feature["_runtime_region_selection"]
    if feature.get("_runtime_extrude_details"):
        merged_details["extrude"] = feature["_runtime_extrude_details"]
    if feature.get("_runtime_extrude_direction_selection"):
        merged_details["extrude_direction_selection"] = feature["_runtime_extrude_direction_selection"]
    if feature.get("_runtime_revolve_tool_selection"):
        merged_details["revolve_tool_selection"] = feature["_runtime_revolve_tool_selection"]
    if current is not None:
        try:
            api_post_state = shape_post_state(current)
            merged_details["simplecadapi_post_state"] = api_post_state
            sw_post_state = feature.get("solidworks_post_state")
            if isinstance(sw_post_state, dict) and "volume_mm3" in sw_post_state:
                merged_details["solidworks_post_state"] = sw_post_state
                merged_details["post_state_delta"] = post_state_delta(api_post_state, sw_post_state)
        except Exception as exc:
            merged_details["simplecadapi_post_state_error"] = repr(exc)
    return {
        "feature_index": feature.get("index"),
        "feature_name": feature.get("name"),
        "feature_type": feature.get("type"),
        "status": status,
        "rule": rule,
        "sketch": {
            "segment_counts": sketch_segment_counts(sketch),
            "coordinate_system": sketch.get("coordinate_system") if sketch else None,
            "runtime_coordinate_system": sketch.get("runtime_coordinate_system") if sketch else None,
            "matrix_mode": sketch.get("runtime_matrix_mode") if sketch else None,
        },
        "simplecadapi_ops": simplecadapi_ops or [],
        "details": merged_details,
    }


def prepare_feature_tree(data: dict[str, Any]) -> tuple[str, bool]:
    hints = data.get("conversion_hints") or {}
    matrix_mode = str(hints.get("matrix_mode") or "raw")
    ignore_extrude_reverse = bool(hints.get("ignore_extrude_reverse", False))
    sheet_metal_context: dict[str, float] = {}
    for feature in data.get("features", []):
        if feature.get("type") == "SMBaseFlange":
            definition = feature.get("definition", {})
            thickness = definition.get("thickness_m")
            bend_radius = definition.get("bend_radius_m")
            if thickness is not None and bend_radius is not None:
                sheet_metal_context = {
                    "thickness_m": float(thickness),
                    "bend_radius_m": float(bend_radius),
                    "outer_radius_m": float(thickness) + float(bend_radius),
                }
                break
    for feature in data.get("features", []):
        if sheet_metal_context:
            feature["_sheet_metal_context"] = sheet_metal_context
        sketches = []
        if feature.get("type") == "ProfileFeature" and "sketch" in feature:
            sketches.append(feature["sketch"])
        for sub in feature.get("subfeatures", []):
            if sub.get("type") == "ProfileFeature" and "sketch" in sub:
                sketches.append(sub["sketch"])
        for sketch in sketches:
            matrix = sketch.get("sketch_to_model_matrix")
            if matrix and matrix_mode == "inverse":
                try:
                    sketch["runtime_matrix"] = np.linalg.inv(np.array(matrix, dtype=float)).tolist()
                    sketch["runtime_matrix_mode"] = "inverse"
                except Exception:
                    sketch["runtime_matrix"] = matrix
                    sketch["runtime_matrix_mode"] = "raw-fallback"
            elif matrix:
                sketch["runtime_matrix"] = matrix
                sketch["runtime_matrix_mode"] = "raw"
            if sketch.get("runtime_matrix"):
                sketch["runtime_coordinate_system"] = coordinate_system_from_matrix(sketch["runtime_matrix"])
    return matrix_mode, ignore_extrude_reverse


def convert_feature_tree(data: dict[str, Any]) -> ConversionResult:
    _, ignore_extrude_reverse = prepare_feature_tree(data)

    current = None
    report: list[str] = []
    operations: list[dict[str, Any]] = []
    for feature in data.get("features", []):
        ftype = feature.get("type")
        name = feature.get("name")
        try:
            if ftype in {"Extrusion", "ICE", "Cut"}:
                sketch = get_feature_sketch(feature)
                if sketch is None:
                    report.append(f"SKIP {name}: no sketch")
                    operations.append(
                        operation_entry(feature, status="skipped", rule="extrude", details={"reason": "no sketch"})
                    )
                    continue
                solids = None
                equivalent_normal_cut = False
                if is_cut_feature(feature) and current is not None:
                    solids = sheet_metal_equivalent_normal_cut_tools(
                        current, sketch, feature, ignore_extrude_reverse, report
                    )
                    equivalent_normal_cut = solids is not None
                if solids is None:
                    solids = auto_extrude_profiles(
                        sketch,
                        feature,
                        ignore_extrude_reverse,
                        current=current,
                    )
                if not solids:
                    report.append(f"SKIP {name}: no closed profile")
                    operations.append(
                        operation_entry(
                            feature,
                            status="skipped",
                            rule="cut_extrude" if is_cut_feature(feature) else "add_extrude",
                            sketch=sketch,
                            details={"reason": "no closed profile"},
                        )
                    )
                    continue
                if is_cut_feature(feature):
                    if current is None:
                        report.append(f"SKIP {name}: cut before base solid")
                        operations.append(
                            operation_entry(
                                feature,
                                status="skipped",
                                rule="cut_extrude",
                                sketch=sketch,
                                details={"reason": "cut before base solid"},
                            )
                        )
                    else:
                        current = safe_cut(current, solids, report)
                        report.append(f"OK cut-extrude {name}: tools={len(solids)}")
                        operations.append(
                            operation_entry(
                                feature,
                                status="ok",
                                rule=feature.get("_runtime_normal_cut_rule", "sheet_metal_equivalent_normal_cut")
                                if equivalent_normal_cut
                                else "cut_extrude",
                                sketch=sketch,
                                simplecadapi_ops=[
                                    "make_segment_redge/make_three_point_arc_redge/make_circle_rwire",
                                    "make_wire_from_edges_rwire/contour_wires/profile_wires/region_wires",
                                    "post_state_direction_candidate_match",
                                    "extrude_rsolid",
                                    "cut_rsolid",
                                ],
                                current=current,
                                details={"tool_count": len(solids)},
                            )
                        )
                else:
                    current = safe_union(
                        current,
                        solids,
                        report,
                        mirror_overlap_plane=mirrored_overlap_plane_for_add(sketch, feature),
                    )
                    report.append(f"OK add-extrude {name}: solids={len(solids)}")
                    operations.append(
                        operation_entry(
                            feature,
                            status="ok",
                            rule="add_extrude",
                            sketch=sketch,
                            simplecadapi_ops=[
                                "make_segment_redge/make_three_point_arc_redge/make_circle_rwire",
                                "make_wire_from_edges_rwire/contour_wires/profile_wires",
                                "post_state_direction_candidate_match",
                                "extrude_rsolid",
                                "union_rsolid",
                            ],
                            current=current,
                            details={"solid_count": len(solids)},
                        )
                    )
            elif ftype in {"Revolution", "RevCut"}:
                sketch = get_feature_sketch(feature)
                if sketch is None:
                    report.append(f"SKIP {name}: no sketch")
                    operations.append(
                        operation_entry(feature, status="skipped", rule="revolve", details={"reason": "no sketch"})
                    )
                    continue
                solids = revolve_profiles(
                    sketch,
                    feature,
                    current=current if is_cut_feature(feature) else None,
                )
                if is_cut_feature(feature):
                    if current is None:
                        report.append(f"SKIP {name}: revcut before base solid")
                        operations.append(
                            operation_entry(
                                feature,
                                status="skipped",
                                rule="revcut",
                                sketch=sketch,
                                details={"reason": "revcut before base solid"},
                            )
                        )
                    else:
                        current = safe_cut(current, solids, report)
                        report.append(f"OK revcut {name}: tools={len(solids)}")
                        operations.append(
                            operation_entry(
                                feature,
                                status="ok",
                                rule="revcut",
                                sketch=sketch,
                                simplecadapi_ops=[
                                    "make_segment_redge/make_three_point_arc_redge",
                                    "make_wire_from_edges_rwire/contour_wires/profile_wires",
                                    "revolve_rsolid",
                                    "mirror_shape/post_state_candidate_match",
                                    "cut_rsolid",
                                ],
                                current=current,
                                details={"tool_count": len(solids)},
                            )
                        )
                else:
                    current = safe_union(current, solids, report)
                    report.append(f"OK revolve {name}: solids={len(solids)}")
                    operations.append(
                        operation_entry(
                            feature,
                            status="ok",
                            rule="add_revolve",
                            sketch=sketch,
                            simplecadapi_ops=[
                                "make_segment_redge/make_three_point_arc_redge",
                                "make_wire_from_edges_rwire/contour_wires/profile_wires",
                                "revolve_rsolid",
                                "union_rsolid",
                            ],
                            current=current,
                            details={"solid_count": len(solids)},
                        )
                    )
            elif ftype == "SMBaseFlange":
                solids = base_flange_from_feature(feature)
                if solids:
                    current = safe_union(current, solids, report)
                    report.append(f"OK base-flange {name}: solids={len(solids)}")
                    operations.append(
                        operation_entry(
                            feature,
                            status="ok",
                            rule="sheet_metal_base_flange",
                            sketch=get_feature_sketch(feature),
                            simplecadapi_ops=[
                                "make_segment_redge/make_three_point_arc_redge",
                                "make_wire_from_edges_rwire",
                                "extrude_rsolid",
                                "union_rsolid",
                            ],
                            current=current,
                            details={"solid_count": len(solids)},
                        )
                    )
                else:
                    report.append(f"SKIP base-flange {name}: unsupported sketch")
                    operations.append(
                        operation_entry(
                            feature,
                            status="skipped",
                            rule="sheet_metal_base_flange",
                            sketch=get_feature_sketch(feature),
                            details={"reason": "unsupported sketch"},
                        )
                    )
        except Exception as exc:
            report.append(f"FAIL {name} ({ftype}): {exc}")
            operations.append(
                operation_entry(feature, status="failed", rule=str(ftype), details={"error": repr(exc)})
            )
    if current is None:
        raise RuntimeError("No solid was generated from feature tree.")
    return ConversionResult(current, report, operations)


def build_from_json(json_path: str | Path) -> ConversionResult:
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    return convert_feature_tree(data)
