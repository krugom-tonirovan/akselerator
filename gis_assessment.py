"""Preliminary GIS checks for candidate shelterbelt lines around an Elista field."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

try:
    import numpy as np
    import rasterio
    from rasterio.warp import transform as reproject_points
except ImportError:  # Keep the weather API available if optional GIS packages are absent.
    np = None
    rasterio = None
    reproject_points = None


ROOT = Path(__file__).resolve().parent
RASTER_DIR = ROOT / "data" / "gis" / "raw"
DEM_PATH = RASTER_DIR / "copernicus_dem_glo30_n46_e044.tif"
LANDCOVER_PATH = RASTER_DIR / "esa_worldcover_2021_v200_n45_e042_map.tif"
SOIL_LAYERS = {
    "clay_pct_0_5cm": ("soilgrids_clay_0-5cm_q050.tif", 10.0),
    "clay_pct_15_30cm": ("soilgrids_clay_15-30cm_q050.tif", 10.0),
    "sand_pct_0_5cm": ("soilgrids_sand_0-5cm_q050.tif", 10.0),
    "sand_pct_15_30cm": ("soilgrids_sand_15-30cm_q050.tif", 10.0),
    "ph_h2o_0_5cm": ("soilgrids_phh2o_0-5cm_q050.tif", 10.0),
    "ph_h2o_15_30cm": ("soilgrids_phh2o_15-30cm_q050.tif", 10.0),
    "organic_carbon_g_kg_0_5cm": ("soilgrids_soc_0-5cm_q050.tif", 10.0),
    "organic_carbon_g_kg_15_30cm": ("soilgrids_soc_15-30cm_q050.tif", 10.0),
}
LANDCOVER_NAMES = {
    10: "Древесная растительность",
    20: "Кустарники",
    30: "Травяной покров",
    40: "Пашня",
    50: "Застройка",
    60: "Редкая растительность / открытая почва",
    70: "Снег и лёд",
    80: "Постоянная вода",
    90: "Водно-болотные угодья",
    95: "Мангровая растительность",
    100: "Мхи и лишайники",
}
HARD_CONFLICT_CLASSES = {50, 80}


def data_status() -> dict[str, Any]:
    layers = {
        "Copernicus DEM GLO-30": DEM_PATH.is_file(),
        "ESA WorldCover 2021": LANDCOVER_PATH.is_file(),
        "SoilGrids 250 m": all((RASTER_DIR / filename).is_file() for filename, _ in SOIL_LAYERS.values()),
    }
    dependencies_ready = rasterio is not None and np is not None and reproject_points is not None
    return {
        "ready": dependencies_ready and all(layers.values()),
        "available": dependencies_ready and any(layers.values()),
        "dependencies_ready": dependencies_ready,
        "layers": layers,
        "constraint_layers": {
            "Кадастровые границы": False,
            "Дорожная сеть и полосы отвода": False,
            "Границы аэропорта и приаэродромные зоны": False,
        },
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "blocked": False,
        "hard_conflict_pct": None,
        "cropland_pct": None,
        "landcover_shares_pct": {},
        "soil": {},
        "warnings": [reason],
    }


def _line_points(line: list[list[float]], spacing_m: float) -> list[tuple[float, float]]:
    if len(line) < 2:
        return []
    points: list[tuple[float, float]] = []
    for start, end in zip(line, line[1:]):
        lon1, lat1 = float(start[0]), float(start[1])
        lon2, lat2 = float(end[0]), float(end[1])
        mid_lat = math.radians((lat1 + lat2) / 2)
        dx = (lon2 - lon1) * 111_320 * math.cos(mid_lat)
        dy = (lat2 - lat1) * 110_574
        length = math.hypot(dx, dy)
        steps = max(1, math.ceil(length / spacing_m))
        segment_points = [
            (lon1 + (lon2 - lon1) * step / steps, lat1 + (lat2 - lat1) * step / steps)
            for step in range(steps + 1)
        ]
        points.extend(segment_points if not points else segment_points[1:])
    return points


def _line_corridor_points(line: list[list[float]], spacing_m: float, half_width_m: float = 15.0) -> list[tuple[float, float]]:
    """Sample a narrow buffer around a planting line to catch nearby hard conflicts."""
    if len(line) < 2:
        return []
    points: list[tuple[float, float]] = []
    offsets = [offset for offset in range(-int(half_width_m), int(half_width_m) + 1, 5)]
    for start, end in zip(line, line[1:]):
        lon1, lat1 = float(start[0]), float(start[1])
        lon2, lat2 = float(end[0]), float(end[1])
        mid_lat = math.radians((lat1 + lat2) / 2)
        meter_lon = 111_320 * max(0.05, math.cos(mid_lat))
        meter_lat = 110_574
        dx = (lon2 - lon1) * meter_lon
        dy = (lat2 - lat1) * meter_lat
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            continue
        steps = max(1, math.ceil(length / spacing_m))
        normal_x, normal_y = -dy / length, dx / length
        for step in range(steps + 1):
            fraction = step / steps
            lon = lon1 + (lon2 - lon1) * fraction
            lat = lat1 + (lat2 - lat1) * fraction
            for offset in offsets:
                points.append((lon + normal_x * offset / meter_lon, lat + normal_y * offset / meter_lat))
    return points


def _transform_points(dataset: Any, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    xs, ys = reproject_points("EPSG:4326", dataset.crs, [p[0] for p in points], [p[1] for p in points])
    return list(zip(xs, ys))


def _sample_values(dataset: Any, points: list[tuple[float, float]]) -> list[float | None]:
    if not points:
        return []
    native_points = _transform_points(dataset, points)
    samples = dataset.sample(native_points, masked=True)
    values: list[float | None] = []
    for sample in samples:
        value = sample[0]
        if np.ma.is_masked(value):
            values.append(None)
            continue
        number = float(value)
        if not math.isfinite(number) or (dataset.nodata is not None and number == dataset.nodata):
            values.append(None)
        else:
            values.append(number)
    return values


def _terrain_summary(dataset: Any, points: list[tuple[float, float]]) -> dict[str, Any]:
    if not points:
        return {"samples": 0, "mean_slope_deg": None, "max_slope_deg": None, "downslope_azimuth_deg": None}
    if not dataset.crs or not dataset.crs.is_geographic:
        return {"samples": 0, "mean_slope_deg": None, "max_slope_deg": None, "downslope_azimuth_deg": None}

    dx = abs(float(dataset.transform.a))
    dy = abs(float(dataset.transform.e))
    centers = _transform_points(dataset, points)
    requests: list[tuple[float, float]] = []
    for x, y in centers:
        requests.extend(((x, y), (x + dx, y), (x - dx, y), (x, y + dy), (x, y - dy)))
    samples = list(dataset.sample(requests, masked=True))
    slopes: list[float] = []
    direction_sin: list[float] = []
    direction_cos: list[float] = []
    for index, (lon, lat) in enumerate(points):
        group = samples[index * 5:(index + 1) * 5]
        values: list[float] = []
        for sample in group:
            value = sample[0]
            if np.ma.is_masked(value):
                values = []
                break
            z = float(value)
            if not math.isfinite(z) or (dataset.nodata is not None and z == dataset.nodata):
                values = []
                break
            values.append(z)
        if len(values) != 5:
            continue
        z_center, z_east, z_west, z_north, z_south = values
        dx_m = dx * 111_320 * max(0.05, math.cos(math.radians(lat)))
        dy_m = dy * 110_574
        if dx_m <= 0 or dy_m <= 0:
            continue
        dz_east = (z_east - z_west) / (2 * dx_m)
        dz_north = (z_north - z_south) / (2 * dy_m)
        slope = math.degrees(math.atan(math.hypot(dz_east, dz_north)))
        slopes.append(slope)
        if slope >= 0.05:
            down_east, down_north = -dz_east, -dz_north
            azimuth = math.atan2(down_east, down_north)
            direction_sin.append(math.sin(azimuth))
            direction_cos.append(math.cos(azimuth))

    mean_direction = None
    if direction_sin:
        mean_direction = math.degrees(math.atan2(sum(direction_sin), sum(direction_cos))) % 360
    return {
        "samples": len(slopes),
        "mean_slope_deg": round(sum(slopes) / len(slopes), 2) if slopes else None,
        "max_slope_deg": round(max(slopes), 2) if slopes else None,
        "downslope_azimuth_deg": round(mean_direction, 1) if mean_direction is not None else None,
    }


def _landcover_summary(dataset: Any, points: list[tuple[float, float]]) -> dict[str, Any]:
    values = _sample_values(dataset, points)
    counts = Counter(int(round(value)) for value in values if value is not None and int(round(value)) != 0)
    total = sum(counts.values())
    if total == 0:
        return {"samples": 0, "shares_pct": {}, "hard_conflict_pct": None, "cropland_pct": None, "blocked": False, "dominant": None}
    shares = {LANDCOVER_NAMES.get(code, f"Класс {code}"): round(count / total * 100, 1) for code, count in counts.items()}
    hard_count = sum(counts.get(code, 0) for code in HARD_CONFLICT_CLASSES)
    cropland_pct = counts.get(40, 0) / total * 100
    dominant_code = counts.most_common(1)[0][0]
    hard_pct = hard_count / total * 100
    return {
        "samples": total,
        "shares_pct": shares,
        "hard_conflict_pct": round(hard_pct, 1),
        "cropland_pct": round(cropland_pct, 1),
        # A single mapped built-up/water hit is enough to avoid recommending
        # a line; narrow roads and runway surfaces can occupy under half of it.
        "blocked": hard_count > 0,
        "dominant": LANDCOVER_NAMES.get(dominant_code, f"Класс {dominant_code}"),
        "counts": dict(counts),
    }


def _unique_soil_points(dataset: Any, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    native_points = _transform_points(dataset, points)
    unique: dict[tuple[int, int], tuple[float, float]] = {}
    for lonlat, native in zip(points, native_points):
        try:
            row, col = dataset.index(*native)
        except (ValueError, OverflowError):
            continue
        unique[(row, col)] = lonlat
    return list(unique.values())


def _soil_summaries(lines: list[list[tuple[float, float]]]) -> tuple[list[dict[str, float]], list[int]]:
    soil_values: list[dict[str, float]] = [{} for _ in lines]
    sample_counts = [0 for _ in lines]
    soil_points = [_line_points_from_parts(points, 250.0) for points in lines]
    for property_name, (filename, scale) in SOIL_LAYERS.items():
        path = RASTER_DIR / filename
        if not path.is_file():
            continue
        try:
            with rasterio.open(path) as dataset:
                for index, points in enumerate(soil_points):
                    unique_points = _unique_soil_points(dataset, points)
                    values = _sample_values(dataset, unique_points)
                    valid = [value / scale for value in values if value is not None]
                    if valid:
                        soil_values[index][property_name] = round(float(np.median(valid)), 2)
                        sample_counts[index] = max(sample_counts[index], len(valid))
        except (OSError, ValueError, RuntimeError):
            continue
    return soil_values, sample_counts


def _line_points_from_parts(points: list[tuple[float, float]], spacing_m: float) -> list[tuple[float, float]]:
    if len(points) < 2:
        return points
    return _line_points([[points[0][0], points[0][1]], [points[-1][0], points[-1][1]]], spacing_m)


def assess_belts(lines: list[list[list[float]]]) -> list[dict[str, Any]]:
    """Sample terrain, land cover, and soil along candidate external planting lines."""
    if rasterio is None or np is None or reproject_points is None:
        return [_unavailable("GIS-проверка недоступна: установите зависимости из requirements.txt.") for _ in lines]

    landcover_points = [_line_corridor_points(line, 5.0, half_width_m=15.0) for line in lines]
    terrain_points = [_line_points(line, 30.0) for line in lines]
    soil_points = [_line_points(line, 250.0) for line in lines]
    terrain_results: list[dict[str, Any]] = [{"samples": 0, "mean_slope_deg": None, "max_slope_deg": None, "downslope_azimuth_deg": None} for _ in lines]
    cover_results: list[dict[str, Any]] = [{"samples": 0, "shares_pct": {}, "hard_conflict_pct": None, "cropland_pct": None, "blocked": False, "dominant": None} for _ in lines]
    soil_results: list[dict[str, float]] = [{} for _ in lines]
    soil_counts = [0 for _ in lines]
    layers_used: list[str] = []

    try:
        if DEM_PATH.is_file():
            with rasterio.open(DEM_PATH) as dataset:
                for index, points in enumerate(terrain_points):
                    terrain_results[index] = _terrain_summary(dataset, points)
            if any(item["samples"] for item in terrain_results):
                layers_used.append("Copernicus DEM GLO-30")
    except (OSError, ValueError, RuntimeError):
        pass

    try:
        if LANDCOVER_PATH.is_file():
            with rasterio.open(LANDCOVER_PATH) as dataset:
                for index, points in enumerate(landcover_points):
                    cover_results[index] = _landcover_summary(dataset, points)
            if any(item["samples"] for item in cover_results):
                layers_used.append("ESA WorldCover 2021")
    except (OSError, ValueError, RuntimeError):
        pass

    soil_results, soil_counts = _soil_summaries(soil_points)
    if any(soil_results):
        layers_used.append("SoilGrids 250 m")

    assessments: list[dict[str, Any]] = []
    for index in range(len(lines)):
        terrain = terrain_results[index]
        cover = cover_results[index]
        soil = soil_results[index]
        available = bool(terrain["samples"] or cover["samples"] or soil)
        warnings: list[str] = []
        if cover["hard_conflict_pct"] is not None and cover["hard_conflict_pct"] > 0:
            warnings.append(f"Застройка или постоянная вода занимает {cover['hard_conflict_pct']:.1f}% проверяемой линии.")
        if cover["blocked"]:
            warnings.append("На линии обнаружены пиксели застройки или постоянной воды; вариант помечен как конфликтный.")
        if cover["cropland_pct"] is not None and cover["cropland_pct"] > 0:
            warnings.append(f"Пашня занимает {cover['cropland_pct']:.1f}% линии; право на эту землю не установлено.")
        if not terrain["samples"]:
            warnings.append("Линия вне покрытия DEM для Элисты или не удалось прочитать высоты.")
        if not cover["samples"]:
            warnings.append("Линия вне покрытия карты земного покрова.")
        if not soil:
            warnings.append("Почвенные значения SoilGrids для линии недоступны.")
        assessments.append({
            "available": available,
            "layers_used": layers_used if available else [],
            "mean_slope_deg": terrain["mean_slope_deg"],
            "max_slope_deg": terrain["max_slope_deg"],
            "downslope_azimuth_deg": terrain["downslope_azimuth_deg"],
            "terrain_samples": terrain["samples"],
            "dominant_landcover": cover["dominant"],
            "landcover_shares_pct": cover["shares_pct"],
            "landcover_samples": cover["samples"],
            "hard_conflict_pct": cover["hard_conflict_pct"],
            "cropland_pct": cover["cropland_pct"],
            "blocked": cover["blocked"],
            "soil": soil,
            "soil_cells": soil_counts[index],
            "warnings": warnings,
        })
    return assessments


def combine_assessments(assessments: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [item for item in assessments if item.get("available")]
    if not usable:
        return _unavailable("Открытые ГИС-слои не покрывают ни одну из предложенных линий.")

    cover_weight = sum(int(item.get("landcover_samples", 0)) for item in usable)
    cover_totals: dict[str, float] = {}
    conflict_total = 0.0
    if cover_weight:
        for item in usable:
            weight = int(item.get("landcover_samples", 0))
            conflict = item.get("hard_conflict_pct")
            if conflict is not None:
                conflict_total += float(conflict) * weight
            for name, share in item.get("landcover_shares_pct", {}).items():
                cover_totals[name] = cover_totals.get(name, 0.0) + float(share) * weight / cover_weight
        hard_conflict_pct: float | None = round(conflict_total / cover_weight, 1)
    else:
        hard_conflict_pct = None

    terrain_weight = sum(int(item.get("terrain_samples", 0)) for item in usable)
    slopes = [(float(item["mean_slope_deg"]), int(item.get("terrain_samples", 0))) for item in usable if item.get("mean_slope_deg") is not None]
    mean_slope = sum(value * weight for value, weight in slopes) / sum(weight for _, weight in slopes) if slopes else None
    max_slope_values = [float(item["max_slope_deg"]) for item in usable if item.get("max_slope_deg") is not None]
    drainage_angles = [math.radians(float(item["downslope_azimuth_deg"])) for item in usable if item.get("downslope_azimuth_deg") is not None]
    drainage = None
    if drainage_angles:
        drainage = math.degrees(math.atan2(sum(math.sin(a) for a in drainage_angles), sum(math.cos(a) for a in drainage_angles))) % 360
    soil: dict[str, float] = {}
    for property_name in SOIL_LAYERS:
        values = [float(item["soil"][property_name]) for item in usable if property_name in item.get("soil", {})]
        if values:
            soil[property_name] = round(float(median(values)), 2)
    warnings = list(dict.fromkeys(warning for item in usable for warning in item.get("warnings", [])))
    blocked_belts = sum(bool(item.get("blocked")) for item in assessments if item.get("available"))
    return {
        "available": True,
        "line_count": len(assessments),
        "layers_used": list(dict.fromkeys(layer for item in usable for layer in item.get("layers_used", []))),
        "mean_slope_deg": round(mean_slope, 2) if mean_slope is not None else None,
        "max_slope_deg": round(max(max_slope_values), 2) if max_slope_values else None,
        "downslope_azimuth_deg": round(drainage, 1) if drainage is not None else None,
        "terrain_samples": terrain_weight,
        "dominant_landcover": max(cover_totals, key=cover_totals.get) if cover_totals else None,
        "landcover_shares_pct": {key: round(value, 1) for key, value in sorted(cover_totals.items(), key=lambda pair: pair[1], reverse=True)},
        "landcover_samples": cover_weight,
        "hard_conflict_pct": hard_conflict_pct,
        "cropland_pct": round(cover_totals.get("Пашня", 0.0), 1) if cover_totals else None,
        "blocked_belts": blocked_belts,
        "blocked": blocked_belts > 0,
        "soil": soil,
        "soil_cells": sum(int(item.get("soil_cells", 0)) for item in usable),
        "warnings": warnings,
    }
