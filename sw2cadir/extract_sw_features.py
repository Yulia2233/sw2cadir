from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import win32com.client
from win32com.client import gencache, makepy


DEFAULT_SOLIDWORKS_TLB = Path(
    os.environ.get("SOLIDWORKS_TLB", r"D:\solidworks\SOLIDWORKS\sldworks.tlb")
)
GEOMETRY_TYPES = {"Extrusion", "ICE", "Cut", "Revolution", "RevCut", "SMBaseFlange"}
SW_SUPPRESS_FEATURE = 0
SW_UNSUPPRESS_FEATURE = 1
SW_THIS_CONFIGURATION = 1


def as_float_tuple(values: Any) -> tuple[float, ...] | None:
    if values is None:
        return None
    try:
        return tuple(float(v) for v in values)
    except Exception:
        return None


def point_xyz(point_obj: Any) -> tuple[float, float, float]:
    return (float(point_obj.X), float(point_obj.Y), float(point_obj.Z))


def mat_from_sw_xform(xform: tuple[float, ...] | None) -> np.ndarray | None:
    if not xform or len(xform) < 12:
        return None
    m = np.eye(4)
    m[0, 0], m[0, 1], m[0, 2] = xform[0], xform[1], xform[2]
    m[1, 0], m[1, 1], m[1, 2] = xform[3], xform[4], xform[5]
    m[2, 0], m[2, 1], m[2, 2] = xform[6], xform[7], xform[8]
    m[0, 3], m[1, 3], m[2, 3] = xform[9], xform[10], xform[11]
    if len(xform) > 12:
        scale = float(xform[12])
        if scale and not math.isclose(scale, 1.0):
            m[:3, :3] *= scale
    return m


def transform_point(m: np.ndarray | None, p: tuple[float, float, float]):
    if m is None:
        return p
    v = np.array([p[0], p[1], p[2], 1.0])
    r = m @ v
    return (float(r[0]), float(r[1]), float(r[2]))


def unit_vector(values: Any) -> tuple[float, float, float]:
    arr = np.array(values, dtype=float)
    norm = float(np.linalg.norm(arr))
    if norm < 1e-12:
        return (0.0, 0.0, 0.0)
    arr = arr / norm
    return (float(arr[0]), float(arr[1]), float(arr[2]))


def coordinate_system_from_matrix(m: np.ndarray | None) -> dict[str, Any] | None:
    if m is None:
        return None
    return {
        "origin_model_m": transform_point(m, (0.0, 0.0, 0.0)),
        "x_axis_model": unit_vector(m[:3, 0]),
        "y_axis_model": unit_vector(m[:3, 1]),
        "z_axis_model": unit_vector(m[:3, 2]),
    }


def safe_call(label: str, fn, default=None):
    try:
        return fn()
    except Exception as exc:
        return {"error": f"{label}: {exc!r}"} if default is None else default


class SolidWorksExtractor:
    def __init__(
        self,
        solidworks_tlb: Path = DEFAULT_SOLIDWORKS_TLB,
        hints_path: Path | None = None,
        visible: bool = False,
    ) -> None:
        if hints_path is not None and hints_path.exists():
            self.conversion_hints = json.loads(hints_path.read_text(encoding="utf-8"))
        else:
            self.conversion_hints = {}
        makepy.GenerateFromTypeLibSpec(str(solidworks_tlb), bForDemand=False)
        self.mod = gencache.GetModuleForProgID("SldWorks.Application")
        raw = win32com.client.Dispatch("SldWorks.Application")
        self.sw = gencache.GetClassForProgID("SldWorks.Application")(raw._oleobj_)
        try:
            self.sw.Visible = bool(visible)
        except Exception:
            pass

    def wrap_feature(self, obj: Any):
        if not obj:
            return None
        return self.mod.IFeature(obj._oleobj_)

    def wrap_sketch(self, obj: Any):
        if not obj:
            return None
        return self.mod.ISketch(obj._oleobj_)

    def wrap_segment(self, obj: Any):
        return self.mod.ISketchSegment(obj._oleobj_)

    def dims(self, feat: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            disp = feat.GetFirstDisplayDimension()
        except Exception:
            return rows
        guard = 0
        while disp and guard < 200:
            try:
                dim = disp.GetDimension2(0)
                rows.append(
                    {
                        "full_name": str(dim.FullName),
                        "name": str(dim.Name),
                        "system_value_m": float(dim.SystemValue),
                    }
                )
            except Exception as exc:
                rows.append({"error": repr(exc)})
            try:
                disp = feat.GetNextDisplayDimension(disp)
            except Exception:
                break
            guard += 1
        return rows

    def face_plane_params(self, face_obj: Any) -> tuple[float, ...] | None:
        if not face_obj:
            return None
        try:
            face = self.mod.IFace2(face_obj._oleobj_)
            surface = self.mod.ISurface(face.GetSurface()._oleobj_)
            if bool(surface.IsPlane()):
                return as_float_tuple(surface.PlaneParams)
        except Exception:
            return None
        return None

    def selection_manager_snapshot(self, model: Any) -> dict[str, Any]:
        try:
            sel = self.mod.ISelectionMgr(model.SelectionManager._oleobj_)
            count = int(sel.GetSelectedObjectCount2(-1))
        except Exception as exc:
            return {"error": repr(exc)}
        rows = []
        for index in range(1, count + 1):
            row: dict[str, Any] = {"index": index}
            row["type_code"] = safe_call(
                "GetSelectedObjectType3", lambda index=index: int(sel.GetSelectedObjectType3(index, -1)), None
            )
            row["mark"] = safe_call(
                "GetSelectedObjectMark", lambda index=index: int(sel.GetSelectedObjectMark(index)), None
            )
            row["selection_point_model_m"] = as_float_tuple(
                safe_call("GetSelectionPoint2", lambda index=index: sel.GetSelectionPoint2(index, -1), None)
            )
            try:
                obj = sel.GetSelectedObject6(index, -1)
                if obj:
                    row["object_dispatch"] = str(obj.__class__.__name__)
            except Exception as exc:
                row["object_error"] = repr(exc)
            rows.append(row)
        return {"count": count, "objects": rows}

    def selected_contours(self, definition_obj: Any, count: int | None) -> list[dict[str, Any]]:
        if not count or count <= 0 or not hasattr(definition_obj, "IGetContours"):
            return []
        try:
            raw_contours = definition_obj.IGetContours(count)
        except Exception as exc:
            return [{"error": f"IGetContours: {exc!r}"}]
        if raw_contours is None:
            return []
        if isinstance(raw_contours, (list, tuple)):
            contour_items = list(raw_contours)
        else:
            try:
                contour_items = list(raw_contours)
            except Exception:
                contour_items = [raw_contours]
        contours = []
        for contour_index, raw_contour in enumerate(contour_items):
            contours.append(self.contour_data(raw_contour, contour_index, None))
        return contours

    def definition_selection_metadata(self, model: Any, definition_obj: Any) -> dict[str, Any]:
        selected_count = (
            safe_call("GetContoursCount", lambda: int(definition_obj.GetContoursCount()), None)
            if hasattr(definition_obj, "GetContoursCount")
            else None
        )
        return {
            "selected_contours_count": selected_count,
            "selected_contours": self.selected_contours(definition_obj, selected_count),
            "feature_scope_bodies_count": safe_call(
                "GetFeatureScopeBodiesCount",
                lambda: int(definition_obj.GetFeatureScopeBodiesCount()),
                None,
            )
            if hasattr(definition_obj, "GetFeatureScopeBodiesCount")
            else None,
            "selection_manager_after_access": self.selection_manager_snapshot(model),
        }

    def body_data(self, raw_body: Any) -> dict[str, Any]:
        try:
            body = self.mod.IBody2(raw_body._oleobj_)
            mass_properties = as_float_tuple(
                safe_call("GetMassProperties", lambda: body.GetMassProperties(1), None)
            )
            return {
                "name": safe_call("Name", lambda: str(body.Name), None),
                "type_code": safe_call("GetType", lambda: int(body.GetType()), None),
                "body_box_m": as_float_tuple(safe_call("GetBodyBox", lambda: body.GetBodyBox(), None)),
                "mass_properties": mass_properties,
                "selection_id": safe_call("GetSelectionId", lambda: str(body.GetSelectionId()), None),
            }
        except Exception as exc:
            return {"error": repr(exc)}

    def feature_scope_metadata(self, definition_obj: Any) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        raw_bodies = safe_call("FeatureScopeBodies", lambda: definition_obj.FeatureScopeBodies, None)
        if raw_bodies:
            body_items = raw_bodies if isinstance(raw_bodies, (list, tuple)) else [raw_bodies]
            for raw_body in body_items:
                rows.append(self.body_data(raw_body))
        return {
            "auto_select": safe_call("AutoSelect", lambda: bool(definition_obj.AutoSelect), None)
            if hasattr(definition_obj, "AutoSelect")
            else None,
            "feature_scope": safe_call("FeatureScope", lambda: bool(definition_obj.FeatureScope), None)
            if hasattr(definition_obj, "FeatureScope")
            else None,
            "assembly_feature_scope": safe_call(
                "AssemblyFeatureScope", lambda: bool(definition_obj.AssemblyFeatureScope), None
            )
            if hasattr(definition_obj, "AssemblyFeatureScope")
            else None,
            "auto_select_components": safe_call(
                "AutoSelectComponents", lambda: bool(definition_obj.AutoSelectComponents), None
            )
            if hasattr(definition_obj, "AutoSelectComponents")
            else None,
            "feature_scope_bodies_count": safe_call(
                "GetFeatureScopeBodiesCount",
                lambda: int(definition_obj.GetFeatureScopeBodiesCount()),
                None,
            )
            if hasattr(definition_obj, "GetFeatureScopeBodiesCount")
            else None,
            "feature_scope_bodies": rows,
        }

    def mass_properties(self, model: Any) -> dict[str, Any]:
        try:
            model.ForceRebuild3(False)
            mp = self.mod.IMassProperty(model.Extension.CreateMassProperty()._oleobj_)
            mp.UseSystemUnits = False
            part_doc = self.mod.IPartDoc(model._oleobj_)
            bbox_mm = [float(x) for x in part_doc.GetPartBox(False)]
            return {
                "volume_mm3": float(mp.Volume),
                "surface_area_mm2": float(mp.SurfaceArea),
                "center_mm": [float(x) for x in mp.CenterOfMass],
                "bbox_mm": bbox_mm,
                "span_mm": [
                    bbox_mm[3] - bbox_mm[0],
                    bbox_mm[4] - bbox_mm[1],
                    bbox_mm[5] - bbox_mm[2],
                ],
            }
        except Exception as exc:
            return {"error": repr(exc)}

    def set_feature_suppression(self, feat: Any, action: int) -> bool:
        try:
            return bool(feat.SetSuppression2(action, SW_THIS_CONFIGURATION, None))
        except Exception:
            try:
                return bool(feat.SetSuppression2(action, SW_THIS_CONFIGURATION, ""))
            except Exception:
                return False

    def add_post_state_mass_properties(
        self, model: Any, geometry_refs: list[tuple[dict[str, Any], Any]]
    ) -> None:
        for feature_index, (row, _) in enumerate(geometry_refs):
            suppressed: list[Any] = []
            for later_row, later_feat in geometry_refs[feature_index + 1 :]:
                if self.set_feature_suppression(later_feat, SW_SUPPRESS_FEATURE):
                    suppressed.append(later_feat)
                else:
                    later_row.setdefault("solidworks_post_state_warnings", []).append(
                        f"could not suppress while measuring post-state for feature {row.get('index')}"
                    )
            row["solidworks_post_state"] = self.mass_properties(model)
            for later_feat in reversed(suppressed):
                self.set_feature_suppression(later_feat, SW_UNSUPPRESS_FEATURE)
            try:
                model.ForceRebuild3(False)
            except Exception:
                pass

    def feature_definition(self, model: Any, feat: Any, feature_type: str) -> dict[str, Any]:
        data: dict[str, Any] = {}
        try:
            raw = feat.GetDefinition()
        except Exception as exc:
            return {"error": f"GetDefinition: {exc!r}"}
        if not raw:
            return {}

        if feature_type in {"Extrusion", "Cut", "ICE"}:
            try:
                ex = self.mod.IExtrudeFeatureData2(raw._oleobj_)
                ex.AccessSelections(model, None)
                data.update(
                    {
                        "kind": "extrude_data",
                        "end_condition_forward": safe_call(
                            "GetEndCondition(True)", lambda: int(ex.GetEndCondition(True)), None
                        ),
                        "end_condition_reverse": safe_call(
                            "GetEndCondition(False)", lambda: int(ex.GetEndCondition(False)), None
                        ),
                        "end_condition_forward_reference_type": safe_call(
                            "GetEndConditionReference(True)",
                            lambda: int(ex.GetEndConditionReference(True)[1]),
                            None,
                        ),
                        "end_condition_reverse_reference_type": safe_call(
                            "GetEndConditionReference(False)",
                            lambda: int(ex.GetEndConditionReference(False)[1]),
                            None,
                        ),
                        "end_condition_forward_face_plane": safe_call(
                            "GetFace(True).PlaneParams",
                            lambda: self.face_plane_params(ex.GetFace(True)),
                            None,
                        ),
                        "end_condition_reverse_face_plane": safe_call(
                            "GetFace(False).PlaneParams",
                            lambda: self.face_plane_params(ex.GetFace(False)),
                            None,
                        ),
                        "depth_forward_m": safe_call(
                            "GetDepth(True)", lambda: float(ex.GetDepth(True)), None
                        ),
                        "depth_reverse_m": safe_call(
                            "GetDepth(False)", lambda: float(ex.GetDepth(False)), None
                        ),
                        "draft_forward_rad": safe_call(
                            "GetDraftAngle(True)", lambda: float(ex.GetDraftAngle(True)), None
                        ),
                        "draft_reverse_rad": safe_call(
                            "GetDraftAngle(False)", lambda: float(ex.GetDraftAngle(False)), None
                        ),
                        "is_thin": safe_call("IsThinFeature", lambda: bool(ex.IsThinFeature()), None),
                        "both_directions": safe_call(
                            "BothDirections", lambda: bool(ex.BothDirections), None
                        ),
                        "reverse_direction": safe_call(
                            "ReverseDirection", lambda: bool(ex.ReverseDirection), None
                        ),
                        "flip_side_to_cut": safe_call(
                            "FlipSideToCut", lambda: bool(ex.FlipSideToCut), None
                        ),
                        "merge": safe_call("Merge", lambda: bool(ex.Merge), None),
                        "normal_cut": safe_call("NormalCut", lambda: bool(ex.NormalCut), None),
                        "from_type": safe_call("FromType", lambda: int(ex.FromType), None),
                        "contours_count": safe_call(
                            "GetContoursCount", lambda: int(ex.GetContoursCount()), None
                        ),
                        "feature_scope_metadata": self.feature_scope_metadata(ex),
                        "selection_metadata": self.definition_selection_metadata(model, ex),
                    }
                )
                try:
                    ex.ReleaseSelectionAccess()
                except Exception:
                    pass
            except Exception as exc:
                data["extrude_error"] = repr(exc)

        if feature_type in {"Revolution", "RevCut"}:
            try:
                rev = self.mod.IRevolveFeatureData2(raw._oleobj_)
                rev.AccessSelections(model, None)
                data.update(
                    {
                        "kind": "revolve_data",
                        "axis_type": safe_call("GetAxisType", lambda: int(rev.GetAxisType()), None),
                        "angle_forward_rad": safe_call(
                            "GetRevolutionAngle(True)",
                            lambda: float(rev.GetRevolutionAngle(True)),
                            None,
                        ),
                        "angle_reverse_rad": safe_call(
                            "GetRevolutionAngle(False)",
                            lambda: float(rev.GetRevolutionAngle(False)),
                            None,
                        ),
                        "is_thin": safe_call("IsThinFeature", lambda: bool(rev.IsThinFeature()), None),
                        "reverse_direction": safe_call(
                            "ReverseDirection", lambda: bool(rev.ReverseDirection), None
                        ),
                        "merge": safe_call("Merge", lambda: bool(rev.Merge), None),
                        "type": safe_call("Type", lambda: int(rev.Type), None),
                        "contours_count": safe_call(
                            "GetContoursCount", lambda: int(rev.GetContoursCount()), None
                        ),
                        "feature_scope_metadata": self.feature_scope_metadata(rev),
                        "selection_metadata": self.definition_selection_metadata(model, rev),
                    }
                )
                try:
                    rev.ReleaseSelectionAccess()
                except Exception:
                    pass
            except Exception as exc:
                data["revolve_error"] = repr(exc)

        if feature_type == "SheetMetal":
            try:
                sm = self.mod.ISheetMetalFeatureData(raw._oleobj_)
                sm.AccessSelections(model, None)
                data.update(
                    {
                        "kind": "sheet_metal_data",
                        "custom_bend_allowance": str(
                            safe_call("GetCustomBendAllowance", lambda: sm.GetCustomBendAllowance(), None)
                        ),
                    }
                )
                try:
                    sm.ReleaseSelectionAccess()
                except Exception:
                    pass
            except Exception as exc:
                data["sheet_metal_error"] = repr(exc)

        if feature_type == "SMBaseFlange":
            try:
                bf = self.mod.IBaseFlangeFeatureData(raw._oleobj_)
                try:
                    bf.AccessSelections(model, None)
                except Exception:
                    pass
                data.update(
                    {
                        "kind": "base_flange_data",
                        "thickness_m": safe_call("Thickness", lambda: float(bf.Thickness), None),
                        "bend_radius_m": safe_call("BendRadius", lambda: float(bf.BendRadius), None),
                        "k_factor": safe_call("KFactor", lambda: float(bf.KFactor), None),
                        "reverse_direction": safe_call(
                            "ReverseDirection", lambda: bool(bf.ReverseDirection), None
                        ),
                        "reverse_thickness": safe_call(
                            "ReverseThickness", lambda: bool(bf.ReverseThickness), None
                        ),
                        "symmetric_thickness": safe_call(
                            "SymmetricThickness", lambda: bool(bf.SymmetricThickness), None
                        ),
                        "d1_end_condition_distance_m": safe_call(
                            "D1EndConditionDistance",
                            lambda: float(bf.D1EndConditionDistance),
                            None,
                        ),
                        "d2_end_condition_distance_m": safe_call(
                            "D2EndConditionDistance",
                            lambda: float(bf.D2EndConditionDistance),
                            None,
                        ),
                        "d1_offset_distance_m": safe_call(
                            "D1OffsetDistance", lambda: float(bf.D1OffsetDistance), None
                        ),
                        "d2_offset_distance_m": safe_call(
                            "D2OffsetDistance", lambda: float(bf.D2OffsetDistance), None
                        ),
                        "offset_directions": safe_call(
                            "OffsetDirections", lambda: int(bf.OffsetDirections), None
                        ),
                        "relief_type": safe_call("ReliefType", lambda: int(bf.ReliefType), None),
                        "relief_width_m": safe_call("ReliefWidth", lambda: float(bf.ReliefWidth), None),
                        "relief_depth_m": safe_call("ReliefDepth", lambda: float(bf.ReliefDepth), None),
                    }
                )
                try:
                    bf.ReleaseSelectionAccess()
                except Exception:
                    pass
            except Exception as exc:
                data["base_flange_error"] = repr(exc)

        return data

    def sketch_segment(self, sketch: Any, raw_segment: Any, sketch_to_model: np.ndarray | None):
        seg = self.wrap_segment(raw_segment)
        segment_type = int(seg.GetType())
        base: dict[str, Any] = {"type_code": segment_type}
        if segment_type == 0:
            line = self.mod.ISketchLine(raw_segment._oleobj_)
            start = point_xyz(self.mod.ISketchPoint(line.GetStartPoint2()._oleobj_))
            end = point_xyz(self.mod.ISketchPoint(line.GetEndPoint2()._oleobj_))
            base.update(
                {
                    "kind": "line",
                    "start_sketch_m": start,
                    "end_sketch_m": end,
                    "start_model_m": transform_point(sketch_to_model, start),
                    "end_model_m": transform_point(sketch_to_model, end),
                    "construction": safe_call(
                        "ConstructionGeometry", lambda: bool(seg.ConstructionGeometry), False
                    ),
                    "is_bend_line": safe_call("IsBendLine", lambda: bool(seg.IsBendLine()), False),
                }
            )
        elif segment_type == 1:
            arc = self.mod.ISketchArc(raw_segment._oleobj_)
            start = point_xyz(self.mod.ISketchPoint(arc.GetStartPoint2()._oleobj_))
            end = point_xyz(self.mod.ISketchPoint(arc.GetEndPoint2()._oleobj_))
            center = point_xyz(self.mod.ISketchPoint(arc.GetCenterPoint2()._oleobj_))
            radius = float(arc.GetRadius())
            full_circle = np.linalg.norm(np.array(start) - np.array(end)) < 1e-9
            base.update(
                {
                    "kind": "circle" if full_circle else "arc",
                    "start_sketch_m": start,
                    "end_sketch_m": end,
                    "center_sketch_m": center,
                    "radius_m": radius,
                    "start_model_m": transform_point(sketch_to_model, start),
                    "end_model_m": transform_point(sketch_to_model, end),
                    "center_model_m": transform_point(sketch_to_model, center),
                    "rotation_dir": safe_call("GetRotationDir", lambda: int(arc.GetRotationDir()), None),
                    "normal_vector": as_float_tuple(
                        safe_call("GetNormalVector", lambda: arc.GetNormalVector(), None)
                    ),
                    "construction": safe_call(
                        "ConstructionGeometry", lambda: bool(seg.ConstructionGeometry), False
                    ),
                    "is_bend_line": safe_call("IsBendLine", lambda: bool(seg.IsBendLine()), False),
                }
            )
        else:
            base["kind"] = f"unsupported_{segment_type}"
        return base

    def contour_data(
        self,
        raw_contour: Any,
        contour_index: int,
        sketch_to_model_m: np.ndarray | None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {"index": contour_index}
        try:
            contour = self.mod.ISketchContour(raw_contour._oleobj_)
            row["edge_count"] = safe_call("GetEdgesCount", lambda: int(contour.GetEdgesCount()), None)
            row["sketch_segment_count"] = safe_call(
                "GetSketchSegmentsCount", lambda: int(contour.GetSketchSegmentsCount()), None
            )
            edges = []
            try:
                raw_edges = contour.GetEdges()
            except Exception:
                raw_edges = None
            if raw_edges:
                for raw_edge in raw_edges:
                    try:
                        edges.append(self.edge_data(raw_edge))
                    except Exception as exc:
                        edges.append({"error": repr(exc)})
            row["edges"] = edges

            segments = []
            try:
                raw_segments = contour.GetSketchSegments()
            except Exception:
                raw_segments = None
            if raw_segments:
                for raw_segment in raw_segments:
                    try:
                        segments.append(self.sketch_segment(None, raw_segment, sketch_to_model_m))
                    except Exception as exc:
                        segments.append({"error": repr(exc)})
            row["segments"] = segments
        except Exception as exc:
            row["error"] = repr(exc)
        return row

    def edge_data(self, raw_edge: Any) -> dict[str, Any]:
        edge = self.mod.IEdge(raw_edge._oleobj_)
        start_v = self.mod.IVertex(edge.GetStartVertex()._oleobj_)
        end_v = self.mod.IVertex(edge.GetEndVertex()._oleobj_)
        curve = self.mod.ICurve(edge.GetCurve()._oleobj_)
        is_circle = safe_call("IsCircle", lambda: bool(curve.IsCircle()), False)
        return {
            "start_model_m": as_float_tuple(start_v.GetPoint()),
            "end_model_m": as_float_tuple(end_v.GetPoint()),
            "is_line": safe_call("IsLine", lambda: bool(curve.IsLine()), False),
            "is_circle": is_circle,
            "curve_params2": as_float_tuple(
                safe_call("GetCurveParams2", lambda: edge.GetCurveParams2(), None)
            ),
            "circle_params": as_float_tuple(
                safe_call("CircleParams", lambda: curve.CircleParams, None)
            )
            if is_circle
            else None,
            "is_param_reversed": safe_call(
                "IsParamReversed", lambda: bool(edge.IsParamReversed()), False
            ),
        }

    def sketch_contours(self, sketch: Any, sketch_to_model_m: np.ndarray | None) -> list[dict[str, Any]]:
        contours = []
        try:
            raw_contours = sketch.GetSketchContours()
        except Exception:
            raw_contours = None
        if not raw_contours:
            return contours
        for contour_index, raw_contour in enumerate(raw_contours):
            contours.append(self.contour_data(raw_contour, contour_index, sketch_to_model_m))
        return contours

    def sketch_data(self, feat: Any) -> dict[str, Any]:
        sketch = self.wrap_sketch(feat.GetSpecificFeature2())
        model_to_sketch = as_float_tuple(safe_call("ModelToSketchXform", lambda: sketch.ModelToSketchXform, None))
        model_to_sketch_m = mat_from_sw_xform(model_to_sketch)
        sketch_to_model_m = model_to_sketch_m

        segments = []
        try:
            raw_segments = sketch.GetSketchSegments()
        except Exception:
            raw_segments = None
        if raw_segments:
            for raw_segment in raw_segments:
                try:
                    segments.append(self.sketch_segment(sketch, raw_segment, sketch_to_model_m))
                except Exception as exc:
                    segments.append({"error": repr(exc)})

        points = []
        try:
            raw_points = sketch.GetSketchPoints2()
        except Exception:
            raw_points = None
        if raw_points:
            for raw_point in raw_points:
                try:
                    p = point_xyz(self.mod.ISketchPoint(raw_point._oleobj_))
                    points.append(
                        {
                            "sketch_m": p,
                            "model_m": transform_point(sketch_to_model_m, p),
                        }
                    )
                except Exception as exc:
                    points.append({"error": repr(exc)})

        regions = []
        try:
            raw_regions = sketch.GetSketchRegions()
        except Exception:
            raw_regions = None
        if raw_regions:
            for raw_region in raw_regions:
                region_edges = []
                try:
                    region = self.mod.ISketchRegion(raw_region._oleobj_)
                    raw_edges = region.GetEdges()
                except Exception:
                    raw_edges = None
                if raw_edges:
                    for raw_edge in raw_edges:
                        try:
                            region_edges.append(self.edge_data(raw_edge))
                        except Exception as exc:
                            region_edges.append({"error": repr(exc)})
                regions.append({"edges": region_edges})

        return {
            "is_3d": safe_call("Is3D", lambda: bool(sketch.Is3D()), None),
            "model_to_sketch_xform": model_to_sketch,
            "raw_transform_matrix": sketch_to_model_m.tolist()
            if sketch_to_model_m is not None
            else None,
            "sketch_to_model_matrix": sketch_to_model_m.tolist()
            if sketch_to_model_m is not None
            else None,
            "coordinate_system": coordinate_system_from_matrix(sketch_to_model_m),
            "segments": segments,
            "points": points,
            "contours": self.sketch_contours(sketch, sketch_to_model_m),
            "regions": regions,
            "region_count": safe_call("GetSketchRegionCount", lambda: int(sketch.GetSketchRegionCount()), None),
            "contour_count": safe_call("GetSketchContourCount", lambda: int(sketch.GetSketchContourCount()), None),
        }

    def extract_file(self, path: Path) -> dict[str, Any]:
        ret = self.sw.OpenDoc6(str(path), 1, 1, "", 0, 0)
        model = ret[0] if isinstance(ret, tuple) else ret
        if not model:
            return {"file": str(path), "open_result": ret, "error": "OpenDoc6 returned no model"}

        features = []
        geometry_refs: list[tuple[dict[str, Any], Any]] = []
        sketches_by_name: dict[str, Any] = {}
        feature_raw = model.FirstFeature()
        index = 0
        while feature_raw is not None and index < 800:
            feat = self.wrap_feature(feature_raw)
            feature_type = str(feat.GetTypeName2())
            row = {
                "index": index,
                "name": str(feat.Name),
                "type": feature_type,
                "dims": self.dims(feat),
                "definition": self.feature_definition(model, feat, feature_type),
            }
            if feature_type == "ProfileFeature":
                row["sketch"] = self.sketch_data(feat)
                sketches_by_name[row["name"]] = row["sketch"]

            subfeatures = []
            try:
                sub_raw = feat.GetFirstSubFeature()
            except Exception:
                sub_raw = None
            guard = 0
            while sub_raw is not None and guard < 100:
                try:
                    sub = self.wrap_feature(sub_raw)
                    sub_row = {
                        "name": str(sub.Name),
                        "type": str(sub.GetTypeName2()),
                        "dims": self.dims(sub),
                    }
                    if sub_row["type"] == "ProfileFeature":
                        sub_row["sketch"] = self.sketch_data(sub)
                        sketches_by_name[sub_row["name"]] = sub_row["sketch"]
                    subfeatures.append(sub_row)
                    sub_raw = sub.GetNextSubFeature()
                except Exception as exc:
                    subfeatures.append({"error": repr(exc)})
                    break
                guard += 1
            row["subfeatures"] = subfeatures
            features.append(row)
            if feature_type in GEOMETRY_TYPES:
                geometry_refs.append((row, feat))
            try:
                feature_raw = feat.GetNextFeature()
            except Exception:
                break
            index += 1

        self.add_post_state_mass_properties(model, geometry_refs)

        data = {
            "file": str(path),
            "title": str(model.GetTitle()),
            "open_errors_warnings": list(ret[1:]) if isinstance(ret, tuple) else [],
            "units": "m",
            "features": features,
            "sketches_by_name": sketches_by_name,
        }
        hint = self.conversion_hints.get(path.stem) or self.conversion_hints.get(data["title"])
        if hint:
            data["conversion_hints"] = hint
        try:
            self.sw.CloseDoc(model.GetTitle())
        except Exception:
            pass
        return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a SOLIDWORKS feature tree into sw2cadir JSON."
    )
    parser.add_argument("files", nargs="+", type=Path, help="Input .SLDPRT files.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("extracted"),
        help="Directory for *.feature_tree.json outputs.",
    )
    parser.add_argument(
        "--hints",
        type=Path,
        default=Path("conversion_hints.json"),
        help="Optional conversion hints JSON.",
    )
    parser.add_argument(
        "--solidworks-tlb",
        type=Path,
        default=DEFAULT_SOLIDWORKS_TLB,
        help="Path to sldworks.tlb. Can also be set with SOLIDWORKS_TLB.",
    )
    parser.add_argument("--visible", action="store_true", help="Show SOLIDWORKS while extracting.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    extractor = SolidWorksExtractor(args.solidworks_tlb, args.hints, args.visible)
    for file_path in args.files:
        file_path = file_path.resolve()
        print(f"extracting {file_path.name}")
        data = extractor.extract_file(file_path)
        out = args.out_dir / f"{file_path.stem}.feature_tree.json"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(out)


if __name__ == "__main__":
    main()
