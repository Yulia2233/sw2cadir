from __future__ import annotations

import argparse
import json
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import win32com.client
from win32com.client import gencache, makepy

from simplecadapi.kernel.ocp_properties import bounding_box, center_of_mass


from sw2cadir.runtime import build_from_json


DEFAULT_SOLIDWORKS_TLB = Path(
    os.environ.get("SOLIDWORKS_TLB", r"D:\solidworks\SOLIDWORKS\sldworks.tlb")
)


def solid_list(part: Any) -> list[Any]:
    return part if isinstance(part, list) else [part]


def build_from_explicit_model(model_path: Path) -> Any:
    module_name = f"_sw_explicit_{model_path.parent.name}"
    spec = importlib.util.spec_from_file_location(module_name, model_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import generated model: {model_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.build_model()


def api_properties(json_path: Path, model_path: Path | None = None) -> dict[str, Any]:
    if model_path is not None and model_path.exists():
        result = build_from_explicit_model(model_path)
        source = str(model_path)
    else:
        result = build_from_json(json_path)
        source = str(json_path)
    solids = solid_list(result.part)
    volumes = [float(s.get_volume()) for s in solids]
    total_volume = sum(volumes)
    centers = [np.array(center_of_mass(s.wrapped).to_tuple(), dtype=float) for s in solids]
    weighted_center = sum(c * v for c, v in zip(centers, volumes)) / total_volume
    boxes = [bounding_box(s.wrapped) for s in solids]
    bbox = [
        min(b.xmin for b in boxes),
        min(b.ymin for b in boxes),
        min(b.zmin for b in boxes),
        max(b.xmax for b in boxes),
        max(b.ymax for b in boxes),
        max(b.zmax for b in boxes),
    ]
    surface_area = sum(sum(float(face.get_area()) for face in s.get_faces()) for s in solids)
    return {
        "volume_mm3": total_volume,
        "surface_area_mm2": surface_area,
        "center_mm": [float(x) for x in weighted_center],
        "bbox_mm": bbox,
        "span_mm": [bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2]],
        "body_count": len(solids),
        "source": source,
        "report": result.report,
    }


def solidworks_properties(sw: Any, part_path: Path) -> dict[str, Any]:
    mod = gencache.GetModuleForProgID("SldWorks.Application")
    ret = sw.OpenDoc6(str(part_path), 1, 1, "", 0, 0)
    model = ret[0] if isinstance(ret, tuple) else ret
    if not model:
        raise RuntimeError(f"OpenDoc6 failed for {part_path}: {ret!r}")
    try:
        model.ForceRebuild3(False)
        mp = mod.IMassProperty(model.Extension.CreateMassProperty()._oleobj_)
        mp.UseSystemUnits = False
        part_doc = mod.IPartDoc(model._oleobj_)
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
    finally:
        try:
            sw.CloseDoc(model.GetTitle())
        except Exception:
            pass


def diff(sw_props: dict[str, Any], api_props: dict[str, Any]) -> dict[str, Any]:
    return {
        "volume_mm3": api_props["volume_mm3"] - sw_props["volume_mm3"],
        "volume_percent": (api_props["volume_mm3"] - sw_props["volume_mm3"])
        / sw_props["volume_mm3"]
        * 100.0,
        "span_mm": [
            api_props["span_mm"][i] - sw_props["span_mm"][i] for i in range(3)
        ],
        "center_mm": [
            api_props["center_mm"][i] - sw_props["center_mm"][i] for i in range(3)
        ],
        # Surface area and center are recorded for diagnostics. They can differ
        # when a kernel keeps touching regions as multiple solids.
        "surface_area_mm2": api_props["surface_area_mm2"] - sw_props["surface_area_mm2"],
    }


def _resolve_manifest_path(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def load_manifest(path: Path) -> list[dict[str, Any]]:
    base = path.resolve().parent
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Verification manifest must be a JSON list.")
    rows: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("Each manifest row must be an object.")
        rows.append(
            {
                "name": row["name"],
                "part": _resolve_manifest_path(base, row["part"]),
                "json": _resolve_manifest_path(base, row["json"]),
                "model": _resolve_manifest_path(base, row.get("model")),
            }
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare generated SimpleCADAPI models against SOLIDWORKS MassProperty."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="JSON list with name, part, json, and optional model fields.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("verification"),
        help="Directory for latest_verification.json.",
    )
    parser.add_argument(
        "--solidworks-tlb",
        type=Path,
        default=DEFAULT_SOLIDWORKS_TLB,
        help="Path to sldworks.tlb. Can also be set with SOLIDWORKS_TLB.",
    )
    parser.add_argument("--visible", action="store_true", help="Show SOLIDWORKS while verifying.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    parts = load_manifest(args.manifest)

    makepy.GenerateFromTypeLibSpec(str(args.solidworks_tlb), bForDemand=False)
    raw = win32com.client.Dispatch("SldWorks.Application")
    sw = gencache.GetClassForProgID("SldWorks.Application")(raw._oleobj_)
    try:
        sw.Visible = bool(args.visible)
    except Exception:
        pass

    rows = []
    for part in parts:
        sw_props = solidworks_properties(sw, part["part"])
        api_props = api_properties(part["json"], part.get("model"))
        rows.append(
            {
                "name": part["name"],
                "solidworks": sw_props,
                "simplecadapi": api_props,
                "delta": diff(sw_props, api_props),
            }
        )
    out = args.out_dir / "latest_verification.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)
    for row in rows:
        delta = row["delta"]
        print(
            f"{row['name']}: volume delta {delta['volume_mm3']:.9f} mm^3 "
            f"({delta['volume_percent']:.9g}%), span delta {delta['span_mm']}"
        )


if __name__ == "__main__":
    main()
