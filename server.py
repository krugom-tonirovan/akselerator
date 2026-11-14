"""Локальный сервер цифрового двойника лесополос."""

from __future__ import annotations

import itertools
import json
import math
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from gis_assessment import assess_belts, combine_assessments, data_status as gis_data_status


ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "model" / "elista_forest.json"
PORT = 8000
FRONTEND_DIST = ROOT / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"
EARTH_RADIUS_M = 6_371_008.8
STATION_LAT = 46.3727
STATION_LON = 44.3301
REGION_NAME = "Элиста, Республика Калмыкия"
MODEL: dict[str, Any] | None = None


def load_model() -> dict[str, Any]:
    global MODEL
    if MODEL is None:
        if not MODEL_PATH.exists():
            raise ValueError("Модель Элисты не найдена. Сначала обучите её командой python tools/train_elista.py.")
        with MODEL_PATH.open("r", encoding="utf-8") as stream:
            MODEL = json.load(stream)
    return MODEL


def rf_predict(features: list[float]) -> float:
    """Run the exported sklearn forest using only the Python standard library."""
    model = load_model()
    total = 0.0
    for tree in model["trees"]:
        node = 0
        while tree["left"][node] != -1:
            feature = tree["feature"][node]
            node = tree["left"][node] if features[feature] <= tree["threshold"][node] else tree["right"][node]
        total += tree["values"][node][1]
    return total / len(model["trees"])


def number(value: Any, label: str, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Поле «{label}» должно быть числом.") from None
    if not math.isfinite(result) or result < low or result > high:
        raise ValueError(f"Поле «{label}» должно быть в диапазоне {low:g}–{high:g}.")
    return result


def weather_features(weather: dict[str, Any], wind_ratio: float = 1.0) -> list[float]:
    temperature = number(weather.get("temperature_c"), "Температура", -50, 60)
    humidity = number(weather.get("humidity"), "Влажность", 0, 100)
    wind = number(weather.get("wind_speed"), "Скорость ветра", 0, 60)
    gust = max(wind, number(weather.get("wind_gust", wind), "Порывы ветра", 0, 75))
    direction = number(weather.get("wind_from", 315), "Направление ветра", 0, 360) % 360
    visibility = number(weather.get("visibility"), "Видимость", 0, 100)
    month_value = number(weather.get("month"), "Месяц сценария", 1, 12)
    if not month_value.is_integer():
        raise ValueError("Месяц сценария должен быть целым числом от 1 до 12.")
    month = int(month_value)

    # Model features mirror the Elista training data and its exact exported order.
    wind_to = math.radians((direction + 180) % 360)
    speed = wind * wind_ratio
    gust_after = gust * wind_ratio
    month_angle = 2 * math.pi * (month - 1) / 12
    return [
        temperature,
        humidity,
        speed,
        gust_after,
        speed * math.sin(wind_to),
        speed * math.cos(wind_to),
        visibility,
        math.sin(month_angle),
        math.cos(month_angle),
    ]


def local_polygon(ring: list[list[float]]) -> tuple[list[tuple[float, float]], float, float]:
    if not isinstance(ring, list) or len(ring) < 4 or len(ring) > 500:
        raise ValueError("Нарисуйте участок из 3–499 вершин.")
    coords: list[tuple[float, float]] = []
    for pair in ring:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            raise ValueError("Участок должен быть задан координатами [долгота, широта].")
        lon = number(pair[0], "Долгота", -180, 180)
        lat = number(pair[1], "Широта", -85, 85)
        coords.append((lon, lat))
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    lon0 = sum(p[0] for p in coords[:-1]) / (len(coords) - 1)
    lat0 = sum(p[1] for p in coords[:-1]) / (len(coords) - 1)
    x_scale = math.cos(math.radians(lat0)) * math.pi / 180 * EARTH_RADIUS_M
    y_scale = math.pi / 180 * EARTH_RADIUS_M
    return [((lon - lon0) * x_scale, (lat - lat0) * y_scale) for lon, lat in coords], lon0, lat0


def polygon_area(coords: list[tuple[float, float]]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]) -> bool:
    eps = 1e-8
    o1, o2, o3, o4 = orientation(a, b, c), orientation(a, b, d), orientation(c, d, a), orientation(c, d, b)
    if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and ((o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)):
        return True
    def on_segment(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> bool:
        return min(p[0], r[0]) - eps <= q[0] <= max(p[0], r[0]) + eps and min(p[1], r[1]) - eps <= q[1] <= max(p[1], r[1]) + eps
    return ((abs(o1) <= eps and on_segment(a, c, b)) or (abs(o2) <= eps and on_segment(a, d, b))
            or (abs(o3) <= eps and on_segment(c, a, d)) or (abs(o4) <= eps and on_segment(c, b, d)))


def has_self_intersection(polygon: list[tuple[float, float]]) -> bool:
    edge_count = len(polygon) - 1
    for i in range(edge_count):
        for j in range(i + 1, edge_count):
            if j == i + 1 or (i == 0 and j == edge_count - 1):
                continue
            if segments_intersect(polygon[i], polygon[i + 1], polygon[j], polygon[j + 1]):
                return True
    return False


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:]):
        if (y1 > y) != (y2 > y):
            at_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < at_x:
                inside = not inside
    return inside


def shelter_spans(polygon_st: list[tuple[float, float]], along: float) -> list[tuple[float, float]]:
    """Crosswind intervals where a proposed line intersects a simple parcel polygon."""
    hits: list[float] = []
    for (s1, t1), (s2, t2) in zip(polygon_st, polygon_st[1:]):
        if (s1 <= along < s2) or (s2 <= along < s1):
            fraction = (along - s1) / (s2 - s1)
            hits.append(t1 + fraction * (t2 - t1))
    hits.sort()
    return [(hits[i], hits[i + 1]) for i in range(0, len(hits) - 1, 2) if hits[i + 1] - hits[i] > 0.5]


def grid_points(
    polygon: list[tuple[float, float]],
    ux: float,
    uy: float,
    vx: float,
    vy: float,
    count: int = 9,
) -> list[tuple[float, float, float, float]]:
    min_x, max_x = min(x for x, _ in polygon), max(x for x, _ in polygon)
    min_y, max_y = min(y for _, y in polygon), max(y for _, y in polygon)
    # s is downwind distance and t is crosswind position in the same basis as the parcel.
    points: list[tuple[float, float, float, float]] = []
    for ix in range(count):
        x = min_x + (ix + 0.5) * (max_x - min_x) / count
        for iy in range(count):
            y = min_y + (iy + 0.5) * (max_y - min_y) / count
            if point_in_polygon((x, y), polygon):
                points.append((x, y, x * ux + y * uy, x * vx + y * vy))
    if len(points) < 8:
        # Skinny parcels can miss the regular grid. Sample their vertices and centroid instead.
        unique = polygon[:-1]
        cx = sum(p[0] for p in unique) / len(unique)
        cy = sum(p[1] for p in unique) / len(unique)
        raw = unique + [(cx, cy)]
        points = [(x, y, x * ux + y * uy, x * vx + y * vy) for x, y in raw]
    return points


def simplify_ring(polygon: list[tuple[float, float]], tolerance_m: float = 8.0) -> list[tuple[float, float]]:
    """Remove lasso jitter while preserving corners and the closed field boundary."""
    points = list(polygon[:-1])
    changed = True
    while changed and len(points) > 3:
        changed = False
        for index in range(len(points)):
            a = points[index - 1]
            b = points[index]
            c = points[(index + 1) % len(points)]
            dx, dy = c[0] - a[0], c[1] - a[1]
            length2 = dx * dx + dy * dy
            if length2 <= 1e-9:
                distance = math.hypot(b[0] - a[0], b[1] - a[1])
            else:
                t = max(0.0, min(1.0, ((b[0] - a[0]) * dx + (b[1] - a[1]) * dy) / length2))
                distance = math.hypot(b[0] - (a[0] + t * dx), b[1] - (a[1] + t * dy))
            if distance <= tolerance_m:
                del points[index]
                changed = True
                break
    return points + [points[0]]


def segment_distance(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 <= 1e-12:
        return math.hypot(point[0] - a[0], point[1] - a[1])
    t = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length2))
    return math.hypot(point[0] - (a[0] + t * dx), point[1] - (a[1] + t * dy))


def line_is_outside_polygon(
    start: tuple[float, float], end: tuple[float, float], polygon: list[tuple[float, float]],
    spacing_m: float = 5.0, min_clearance_m: float = 2.0,
) -> bool:
    """Reject an external planting segment if it enters or hugs the selected field."""
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(2, math.ceil(length / spacing_m))
    edges = list(zip(polygon, polygon[1:]))
    for index in range(steps + 1):
        fraction = index / steps
        point = (start[0] + fraction * (end[0] - start[0]), start[1] + fraction * (end[1] - start[1]))
        if point_in_polygon(point, polygon):
            return False
        if min(segment_distance(point, a, b) for a, b in edges) < min_clearance_m:
            return False
    return True


def simulate(payload: dict[str, Any]) -> dict[str, Any]:
    model = load_model()
    reference_scenario = model.get("reference_scenario")
    supplied_weather = payload.get("weather")
    automatic_weather = not isinstance(supplied_weather, dict)
    if automatic_weather and reference_scenario is None:
        raise ValueError("В модели нет автоматического погодного сценария. Переобучите модель из папки проекта.")
    weather = supplied_weather if not automatic_weather else reference_scenario
    design = payload.get("design") or {}
    if not isinstance(weather, dict) or not isinstance(design, dict):
        raise ValueError("Не удалось загрузить погодный сценарий или параметры лесополосы.")

    raw_count = str(design.get("strips", "auto")).strip().lower()
    requested_count: int | None = None
    if raw_count not in {"", "auto", "авто"}:
        try:
            count_value = float(raw_count)
        except (TypeError, ValueError):
            raise ValueError("\u0427\u0438\u0441\u043b\u043e \u043b\u0435\u0441\u043e\u043f\u043e\u043b\u043e\u0441 \u0434\u043e\u043b\u0436\u043d\u043e \u0431\u044b\u0442\u044c \u043f\u043e\u043b\u043e\u0436\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u043c \u0446\u0435\u043b\u044b\u043c \u0447\u0438\u0441\u043b\u043e.") from None
        if not math.isfinite(count_value) or count_value < 1 or not count_value.is_integer():
            raise ValueError("\u0427\u0438\u0441\u043b\u043e \u043b\u0435\u0441\u043e\u043f\u043e\u043b\u043e\u0441 \u0434\u043e\u043b\u0436\u043d\u043e \u0431\u044b\u0442\u044c \u043f\u043e\u043b\u043e\u0436\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u043c \u0446\u0435\u043b\u044b\u043c \u0447\u0438\u0441\u043b\u043e.")
        requested_count = int(count_value)
    height = number(design.get("height", 15), "Высота деревьев", 4, 35)
    porosity = number(design.get("porosity", 0.35), "Ажурность", 0.1, 0.8)
    setback_m = 10.0
    try:
        ring = payload["polygon"]
    except (KeyError, TypeError):
        raise ValueError("Сначала обведите участок лассо на карте.") from None

    polygon, lon0, lat0 = local_polygon(ring)
    if has_self_intersection(polygon):
        raise ValueError("Контур пересекает сам себя. Перерисуйте участок одной замкнутой линией.")
    area_m2 = polygon_area(polygon)
    if area_m2 < 2500:
        raise ValueError("Площадь участка должна быть не меньше 0,25 га.")

    station_lat, station_lon = STATION_LAT, STATION_LON
    dlat = math.radians(lat0 - station_lat)
    dlon = math.radians(lon0 - station_lon)
    haversine = math.sin(dlat / 2) ** 2 + math.cos(math.radians(station_lat)) * math.cos(math.radians(lat0)) * math.sin(dlon / 2) ** 2
    station_distance_km = 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(haversine))) / 1000

    direction_names = ["\u0421", "\u0421\u0412", "\u0412", "\u042e\u0412", "\u042e", "\u042e\u0417", "\u0417", "\u0421\u0417"]
    if automatic_weather:
        direction_entries = reference_scenario.get("direction_counts") or [{
            "wind_from": reference_scenario.get("wind_from", 135),
            "sector_name": direction_names[round(float(reference_scenario.get("wind_from", 135)) / 45) % 8],
            "count": reference_scenario.get("event_count", 1),
        }]
        wind_scenarios = []
        for entry in direction_entries:
            count = int(entry.get("count", 0))
            if count <= 0:
                continue
            scenario_weather = dict(reference_scenario)
            scenario_weather["wind_from"] = float(entry["wind_from"])
            wind_scenarios.append({
                "weather": scenario_weather,
                "wind_from": float(entry["wind_from"]) % 360,
                "sector_name": direction_names[round(float(entry["wind_from"]) / 45) % 8],
                "event_count": count,
            })
    else:
        direction = number(weather.get("wind_from", 315), "Направление ветра", 0, 360) % 360
        manual_weather = dict(weather)
        manual_weather["wind_from"] = direction
        wind_scenarios = [{"weather": manual_weather, "wind_from": direction, "sector_name": direction_names[round(direction / 45) % 8], "event_count": 1}]

    if not wind_scenarios:
        raise ValueError("В архиве модели нет направлений пыльных бурь для расчёта.")
    total_events = sum(item["event_count"] for item in wind_scenarios)
    for item in wind_scenarios:
        item["weight"] = item["event_count"] / total_events

    planning_polygon = simplify_ring(polygon, tolerance_m=8.0)
    if has_self_intersection(planning_polygon) or polygon_area(planning_polygon) < area_m2 * 0.7:
        planning_polygon = polygon
    signed_area = sum(
        a[0] * b[1] - b[0] * a[1]
        for a, b in zip(planning_polygon, planning_polygon[1:])
    )
    ring_is_ccw = signed_area > 0
    candidates: list[dict[str, Any]] = []
    candidate_lookup: dict[tuple[tuple[int, int], tuple[int, int]], int] = {}
    min_edge_length_m = max(35.0, height * 2.0)
    for scenario in wind_scenarios:
        direction = scenario["wind_from"]
        wind_to = math.radians((direction + 180) % 360)
        downwind = (math.sin(wind_to), math.cos(wind_to))
        source = (-downwind[0], -downwind[1])
        proposals: list[tuple[float, dict[str, Any]]] = []
        for edge_index, (start, end) in enumerate(zip(planning_polygon, planning_polygon[1:])):
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = math.hypot(dx, dy)
            if length < min_edge_length_m:
                continue
            tangent = (dx / length, dy / length)
            outward = (dy / length, -dx / length) if ring_is_ccw else (-dy / length, dx / length)
            alignment = outward[0] * source[0] + outward[1] * source[1]
            if alignment < 0.25:
                continue
            trim = min(setback_m * 0.5, length * 0.12)
            a = (
                start[0] + tangent[0] * trim + outward[0] * setback_m,
                start[1] + tangent[1] * trim + outward[1] * setback_m,
            )
            b = (
                end[0] - tangent[0] * trim + outward[0] * setback_m,
                end[1] - tangent[1] * trim + outward[1] * setback_m,
            )
            if not line_is_outside_polygon(a, b, polygon, min_clearance_m=5.0):
                continue
            length_out = math.hypot(b[0] - a[0], b[1] - a[1])
            if length_out < min_edge_length_m * 0.65:
                continue
            belt = {
                "a": a,
                "b": b,
                "length_m": length_out,
                "offset_m": setback_m,
                "axis_name": "field_edge",
                "axis_vector": tangent,
                "axis_azimuth_deg": math.degrees(math.atan2(tangent[0], tangent[1])) % 180,
                "wind_alignment": alignment,
                "edge_index": edge_index,
                "source_sectors": [{"wind_from": direction, "sector_name": scenario["sector_name"], "event_count": scenario["event_count"]}],
            }
            proposals.append((alignment * math.sqrt(length_out), belt))
        # Limit each weather sector to its strongest real boundary segments. This
        # avoids combinations of tiny lasso edges while retaining irregular sides.
        proposals.sort(key=lambda item: (item[0], item[1]["length_m"]), reverse=True)
        for _, belt in proposals[:4]:
            endpoints = sorted(((round(belt["a"][0] / 5), round(belt["a"][1] / 5)), (round(belt["b"][0] / 5), round(belt["b"][1] / 5))))
            key = (endpoints[0], endpoints[1])
            if key in candidate_lookup:
                candidates[candidate_lookup[key]]["source_sectors"].extend(belt["source_sectors"])
            else:
                candidate_lookup[key] = len(candidates)
                candidates.append(belt)

    if not candidates:
        raise ValueError("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u0441\u0442\u0440\u043e\u0438\u0442\u044c \u043b\u0435\u0441\u043e\u043f\u043e\u043b\u043e\u0441\u0443 \u043f\u043e \u0440\u0435\u0430\u043b\u044c\u043d\u043e\u0439 \u0433\u0440\u0430\u043d\u0438\u0446\u0435 \u0443\u0447\u0430\u0441\u0442\u043a\u0430 \u0434\u043b\u044f \u044d\u0442\u0438\u0445 \u043d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0439 \u0432\u0435\u0442\u0440\u0430. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u043a\u043e\u043d\u0442\u0443\u0440 \u0438 \u043f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0441\u043d\u043e\u0432\u0430.")
    candidate_lines = [
        [geographic_xy(*belt["a"], lon0, lat0), geographic_xy(*belt["b"], lon0, lat0)]
        for belt in candidates
    ]
    try:
        candidate_gis = assess_belts(candidate_lines)
    except Exception as exc:
        candidate_gis = [{
            "available": False,
            "reason": f"ГИС-слои не удалось прочитать: {exc}",
            "blocked": False,
            "hard_conflict_pct": None,
            "landcover_shares_pct": {},
            "soil": {},
            "warnings": ["ГИС-слои не удалось прочитать; размещение сравнивается только по погодной модели."],
        } for _ in candidates]

    # Avoid mapped buildings/water and corridors that are mostly cultivated land.
    usable_indices = [
        index for index, assessment in enumerate(candidate_gis)
        if not (
            assessment.get("landcover_samples") and (
                assessment.get("blocked")
                or (assessment.get("cropland_pct") is not None and assessment["cropland_pct"] >= 50.0)
            )
        )
    ]
    if not usable_indices:
        raise ValueError("\u041d\u0430 \u043a\u0430\u0440\u0442\u0435 \u0432\u0441\u0435 \u0432\u043d\u0435\u0448\u043d\u0438\u0435 \u043a\u043e\u0440\u0438\u0434\u043e\u0440\u044b \u043f\u043e\u043f\u0430\u0434\u0430\u044e\u0442 \u043d\u0430 \u0437\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0443, \u0432\u043e\u0434\u0443 \u0438\u043b\u0438 \u0437\u0435\u043c\u043b\u044e \u0441 \u043f\u0440\u0438\u0437\u043d\u0430\u043a\u0430\u043c\u0438 \u043f\u0430\u0448\u043d\u0438. \u0411\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u044b\u0439 \u0432\u0430\u0440\u0438\u0430\u043d\u0442 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d; \u043f\u0440\u0430\u0432\u0430 \u043d\u0430 \u0437\u0435\u043c\u043b\u044e \u043a\u0430\u0440\u0442\u0430 \u043d\u0435 \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u044f\u0435\u0442.")
    candidates = [candidates[index] for index in usable_indices]
    candidate_gis = [candidate_gis[index] for index in usable_indices]
    candidate_lines = [candidate_lines[index] for index in usable_indices]

    points = grid_points(polygon, 1.0, 0.0, 0.0, 1.0, 17)
    # For every historical direction and candidate outer boundary line, precompute
    # shelter at each grid point for cumulative plans and manual selections.
    impacts: dict[tuple[int, int], dict[str, list[float]]] = {}
    baseline_by_scenario: list[float] = []
    for scenario_index, scenario in enumerate(wind_scenarios):
        scenario_weather = scenario["weather"]
        baseline = rf_predict(weather_features(scenario_weather))
        baseline_by_scenario.append(baseline)
        wind_to = math.radians((scenario["wind_from"] + 180) % 360)
        ux, uy = math.sin(wind_to), math.cos(wind_to)
        ray_x, ray_y = -ux, -uy
        for candidate_index, belt in enumerate(candidates):
            ax, ay = belt["a"]
            bx, by = belt["b"]
            edge_x, edge_y = bx - ax, by - ay
            ratios: list[float] = []
            risks: list[float] = []
            denominator = ray_x * edge_y - ray_y * edge_x
            for x, y, _, _ in points:
                distance = None
                fraction = None
                if abs(denominator) > 1e-9:
                    qx, qy = ax - x, ay - y
                    distance = (qx * edge_y - qy * edge_x) / denominator
                    fraction = (qx * ray_y - qy * ray_x) / denominator
                if distance is not None and 0 < distance <= height * 20 and fraction is not None and -1e-6 <= fraction <= 1 + 1e-6:
                    ratio = porosity + (1 - porosity) * (1 - math.exp(-0.15 * distance / height))
                    risk = rf_predict(weather_features(scenario_weather, ratio))
                else:
                    ratio, risk = 1.0, baseline
                ratios.append(ratio)
                risks.append(risk)
            impacts[(candidate_index, scenario_index)] = {"ratios": ratios, "risks": risks}

    baseline_risk = sum(scenario["weight"] * baseline_by_scenario[index] for index, scenario in enumerate(wind_scenarios))

    def evaluate_belts(chosen: list[int]) -> dict[str, Any]:
        weighted_risk = 0.0
        weighted_ratio = 0.0
        protected_share = 0.0
        for scenario_index, scenario in enumerate(wind_scenarios):
            impact_rows = [impacts[(candidate_index, scenario_index)] for candidate_index in chosen]
            scenario_risk = 0.0
            scenario_ratio = 0.0
            protected_points = 0
            for point_index in range(len(points)):
                best_ratio = 1.0
                best_risk = baseline_by_scenario[scenario_index]
                for impact in impact_rows:
                    ratio = impact["ratios"][point_index]
                    if ratio < best_ratio:
                        best_ratio = ratio
                        best_risk = impact["risks"][point_index]
                scenario_risk += best_risk
                scenario_ratio += best_ratio
                if best_ratio < 1.0:
                    protected_points += 1
            weighted_risk += scenario["weight"] * scenario_risk / len(points)
            weighted_ratio += scenario["weight"] * scenario_ratio / len(points)
            protected_share += scenario["weight"] * protected_points / len(points)
        crop_values = [
            float(candidate_gis[index]["cropland_pct"])
            for index in chosen
            if candidate_gis[index].get("cropland_pct") is not None
        ]
        return {
            "positions": list(chosen),
            "risk": weighted_risk,
            "wind_ratio": weighted_ratio,
            "protected_share": protected_share,
            "length_m": sum(candidates[index]["length_m"] for index in chosen),
            "gis_unassessed": sum(not bool(candidate_gis[index].get("landcover_samples")) for index in chosen),
            "gis_conflicts": sum(bool(candidate_gis[index].get("blocked")) for index in chosen),
            "cropland_pct": sum(crop_values) / len(crop_values) if crop_values else None,
        }

    # Grow the plan until it reaches the 15% coverage target or no line adds protection.
    # Below the target, retain even small positive gains so several belts can combine.
    best_by_count: dict[int, dict[str, Any]] = {}
    chosen: list[int] = []
    current = evaluate_belts(chosen)
    for _ in range(len(candidates)):
        ranked: list[tuple[tuple[Any, ...], int, dict[str, Any]]] = []
        for candidate_index in range(len(candidates)):
            if candidate_index in chosen:
                continue
            trial = evaluate_belts(chosen + [candidate_index])
            key = (
                trial["gis_unassessed"], trial["gis_conflicts"], trial["risk"],
                trial["cropland_pct"] if trial["cropland_pct"] is not None else 100.0,
                trial["length_m"], trial["positions"],
            )
            ranked.append((key, candidate_index, trial))
        if not ranked:
            break
        ranked.sort(key=lambda item: item[0])
        _, next_index, next_choice = ranked[0]
        risk_gain = current["risk"] - next_choice["risk"]
        coverage_gain = next_choice["protected_share"] - current["protected_share"]
        if risk_gain <= 1e-9 and coverage_gain <= 1e-9:
            break
        if chosen and current["protected_share"] >= 0.15:
            if risk_gain < max(0.005, baseline_risk * 0.005) and coverage_gain < 0.02:
                break
        chosen.append(next_index)
        current = next_choice
        best_by_count[len(chosen)] = current

    recommended_belt_ids = list(chosen)
    selected_belts_provided = "selected_belts" in design
    selected_belt_ids = recommended_belt_ids
    if selected_belts_provided:
        raw_selected = design.get("selected_belts")
        if not isinstance(raw_selected, list):
            raise ValueError("selected_belts must be a list of belt ids.")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in raw_selected):
            raise ValueError("Belt ids must be integers.")
        selected_belt_ids = list(dict.fromkeys(raw_selected))
        if any(value < 0 or value >= len(candidates) for value in selected_belt_ids):
            raise ValueError("A selected belt id is outside the available candidate list.")
        selected_count = len(selected_belt_ids)
        selected_choice = evaluate_belts(selected_belt_ids)
        option_choices = {selected_count: selected_choice}
    else:
        selected_count = requested_count if requested_count is not None else len(recommended_belt_ids)
        if selected_count == 0 and not best_by_count:
            selected_choice = evaluate_belts([])
            option_choices = {0: selected_choice}
        else:
            if selected_count not in best_by_count:
                maximum_available = max(best_by_count, default=0)
                raise ValueError(
                    f"\u0414\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u043a\u043e\u043d\u0442\u0443\u0440\u0430 \u043c\u043e\u0434\u0435\u043b\u044c \u0440\u0430\u0441\u0441\u0447\u0438\u0442\u0430\u043b\u0430 \u0432\u0430\u0440\u0438\u0430\u043d\u0442\u044b \u0434\u043e {maximum_available} \u043b\u0438\u043d\u0438\u0439. "
                    "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0430\u0432\u0442\u043e\u043f\u043e\u0434\u0431\u043e\u0440 \u0438\u043b\u0438 \u0438\u0437\u043c\u0435\u043d\u0438\u0442\u0435 \u043a\u043e\u043d\u0442\u0443\u0440."
                )
            selected_choice = best_by_count[selected_count]
            selected_belt_ids = list(selected_choice["positions"])
            option_choices = best_by_count

    selection_mode = "manual" if selected_belts_provided or requested_count is not None else "auto"
    if selected_belts_provided and selected_count == 0:
        selection_reason = f"\u0412\u044b \u0432\u044b\u043a\u043b\u044e\u0447\u0438\u043b\u0438 \u0432\u0441\u0435 \u043b\u0435\u0441\u043e\u043f\u043e\u043b\u043e\u0441\u044b. \u0418\u043d\u0434\u0435\u043a\u0441 \u0431\u0435\u0437 \u043f\u043e\u0441\u0430\u0434\u043e\u043a: {round(selected_choice['risk'] * 100)}/100."
    elif selected_belts_provided:
        selection_reason = f"\u0412\u043a\u043b\u044e\u0447\u0435\u043d\u043e \u043b\u0438\u043d\u0438\u0439 \u043f\u043e\u0441\u0430\u0434\u043a\u0438: {selected_count}. \u0420\u0430\u0441\u0447\u0451\u0442\u043d\u044b\u0439 \u0438\u043d\u0434\u0435\u043a\u0441: {round(selected_choice['risk'] * 100)}/100; \u0434\u043e\u043b\u044f \u0437\u0430\u0449\u0438\u0442\u044b: {round(selected_choice['protected_share'] * 100)}%."
    elif selected_count == 0:
        selection_reason = (
            "\u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0431\u043e\u0440 \u043d\u0435 \u043d\u0430\u0448\u0451\u043b \u043b\u0438\u043d\u0438\u0439 \u0441 \u0438\u0437\u043c\u0435\u0440\u0438\u043c\u043e\u0439 \u0437\u043e\u043d\u043e\u0439 \u0437\u0430\u0449\u0438\u0442\u044b; "
            "\u043f\u043e\u043a\u0430\u0437\u0430\u043d \u0438\u0441\u0445\u043e\u0434\u043d\u044b\u0439 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439. \u041b\u0438\u043d\u0438\u0438 \u043d\u0438\u0436\u0435 \u043c\u043e\u0436\u043d\u043e \u0432\u044b\u0431\u0440\u0430\u0442\u044c \u0432\u0440\u0443\u0447\u043d\u0443\u044e."
        )
    elif selected_choice["protected_share"] < 0.15:
        selection_reason = (
            "\u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0431\u043e\u0440 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u043b\u0441\u044f: \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0430\u044f \u043b\u0438\u043d\u0438\u044f \u0434\u0430\u0432\u0430\u043b\u0430 \u043c\u0435\u043d\u044c\u0448\u0435 0.5 \u0431\u0430\u043b\u043b\u0430 \u0441\u043d\u0438\u0436\u0435\u043d\u0438\u044f \u0438\u043d\u0434\u0435\u043a\u0441\u0430 "
            "\u0438 \u043c\u0435\u043d\u044c\u0448\u0435 2 \u043f.\u043f. \u0434\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u043e\u0433\u043e \u043f\u043e\u043a\u0440\u044b\u0442\u0438\u044f. "
            "\u041d\u0435\u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0435 \u043b\u0438\u043d\u0438\u0438 \u043c\u043e\u0436\u043d\u043e \u0432\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u043d\u0438\u0436\u0435 \u0438 \u0441\u0440\u0430\u0432\u043d\u0438\u0442\u044c."
        )
    else:
        selection_reason = (
            "\u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0431\u043e\u0440 \u0434\u043e\u0431\u0430\u0432\u043b\u044f\u043b \u043b\u0438\u043d\u0438\u0438 \u043f\u043e \u0432\u043a\u043b\u0430\u0434\u0443 \u0432 \u0437\u0430\u0449\u0438\u0442\u0443 \u0438 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u043b\u0441\u044f, "
            "\u043a\u043e\u0433\u0434\u0430 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0430\u044f \u043f\u043e\u043b\u043e\u0441\u0430 \u0434\u0430\u0432\u0430\u043b\u0430 \u043c\u0430\u043b\u044b\u0439 \u043f\u0440\u0438\u0440\u043e\u0441\u0442. "
            "\u041d\u0438\u0436\u0435 \u043c\u043e\u0436\u043d\u043e \u0432\u043a\u043b\u044e\u0447\u0430\u0442\u044c \u0438 \u043e\u0442\u043a\u043b\u044e\u0447\u0430\u0442\u044c \u043b\u044e\u0431\u044b\u0435 \u043b\u0438\u043d\u0438\u0438; \u043f\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u0438 \u043f\u0435\u0440\u0435\u0441\u0447\u0438\u0442\u0430\u044e\u0442\u0441\u044f \u0434\u043b\u044f \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u043e\u0433\u043e \u043d\u0430\u0431\u043e\u0440\u0430."
        )

    gis_available = any(item.get("landcover_samples") for item in candidate_gis)
    least_unassessed = selected_choice["gis_unassessed"]
    least_gis_conflicts = selected_choice["gis_conflicts"]
    selected_axis_azimuth = candidates[selected_belt_ids[0]]["axis_azimuth_deg"] if selected_belt_ids else 0.0
    result_options: list[dict[str, Any]] = []

    for option_id, (strip_count, choice) in enumerate(sorted(option_choices.items()), start=1):
        line_coordinates: list[list[list[float]]] = []
        zone_coordinates: list[list[list[float]]] = []
        zone_directions: list[dict[str, Any]] = []
        belt_sectors: list[list[str]] = []
        belt_sector_counts: list[list[dict[str, Any]]] = []
        for candidate_index in choice["positions"]:
            belt = candidates[candidate_index]
            ax, ay = belt["a"]
            bx, by = belt["b"]
            line_coordinates.append([geographic_xy(ax, ay, lon0, lat0), geographic_xy(bx, by, lon0, lat0)])
            dominant_sector = max(belt["source_sectors"], key=lambda item: item["event_count"])
            wind_to_deg = (dominant_sector["wind_from"] + 180) % 360
            wind_to = math.radians(wind_to_deg)
            ux, uy = math.sin(wind_to), math.cos(wind_to)
            reach_x, reach_y = ux * height * 20, uy * height * 20
            zone_coordinates.append([
                geographic_xy(ax, ay, lon0, lat0),
                geographic_xy(bx, by, lon0, lat0),
                geographic_xy(bx + reach_x, by + reach_y, lon0, lat0),
                geographic_xy(ax + reach_x, ay + reach_y, lon0, lat0),
                geographic_xy(ax, ay, lon0, lat0),
            ])
            belt_sectors.append([entry["sector_name"] for entry in belt["source_sectors"]])
            belt_sector_counts.append([{ "sector_name": entry["sector_name"], "count": entry["event_count"] } for entry in belt["source_sectors"]])
            zone_directions.append({
                "from_sector": dominant_sector["sector_name"],
                "to_sector": direction_names[round(wind_to_deg / 45) % 8],
                "count": dominant_sector["event_count"],
            })
        reduction = max(0.0, (baseline_risk - choice["risk"]) / baseline_risk * 100) if baseline_risk > 1e-9 else 0.0
        mode_wind = max(wind_scenarios, key=lambda item: item["event_count"])["wind_from"]
        mode_w = math.radians((mode_wind + 180) % 360)
        mode_ux, mode_uy = math.sin(mode_w), math.cos(mode_w)
        if choice["positions"]:
            axis_bx, axis_by = candidates[choice["positions"][0]]["axis_vector"]
            axis_wind_angle = math.degrees(math.acos(max(-1.0, min(1.0, abs(axis_bx * mode_ux + axis_by * mode_uy)))))
        else:
            axis_wind_angle = 0.0
        result_options.append({
            "id": option_id,
            "strips": strip_count,
            "belt_ids": list(choice["positions"]),
            "risk": choice["risk"],
            "reduction_pct": reduction,
            "mean_wind_ratio": choice["wind_ratio"],
            "protected_share": choice["protected_share"],
            "length_m": choice["length_m"],
            "axis_azimuth_deg": candidates[choice["positions"][0]]["axis_azimuth_deg"] if choice["positions"] else 0.0,
            "wind_angle_deg": axis_wind_angle,
            "offsets_m": [candidates[index]["offset_m"] for index in choice["positions"]],
            "belt_sectors": belt_sectors,
            "belt_sector_counts": belt_sector_counts,
            "zone_directions": zone_directions,
            "recommended": not selected_belts_provided and strip_count == selected_count,
            "lines": line_coordinates,
            "zones": zone_coordinates,
            "gis_assessment": combine_assessments([candidate_gis[index] for index in choice["positions"]]),
            "gis_lines": [candidate_gis[index] for index in choice["positions"]],
        })

    selected_option = next(option for option in result_options if option["strips"] == selected_count)
    available_belts = [
        {
            "id": index,
            "length_m": candidates[index]["length_m"],
            "axis_azimuth_deg": candidates[index]["axis_azimuth_deg"],
            "wind_sectors": list(dict.fromkeys(entry["sector_name"] for entry in candidates[index]["source_sectors"])),
            "wind_sector_counts": [{ "sector_name": entry["sector_name"], "count": entry["event_count"] } for entry in candidates[index]["source_sectors"]],
            "line": candidate_lines[index],
            "gis_assessment": candidate_gis[index],
        }
        for index in range(len(candidates))
    ]
    mode_direction = reference_scenario.get("wind_from", 135) if automatic_weather else weather.get("wind_from", 315)
    mode_name = direction_names[round(float(mode_direction) / 45) % 8]
    scenario_result = dict(weather)
    scenario_result["wind_from"] = float(mode_direction)
    scenario_result["source"] = "архив эпизодов пыльных бурь Элисты, 2010–2024" if automatic_weather else "ручной сценарий"
    scenario_result["event_count"] = reference_scenario.get("event_count", 0) if automatic_weather else 0
    scenario_result["period"] = reference_scenario.get("period", "2010–2024") if automatic_weather else ""
    scenario_result["basis"] = (
        "\u041f\u043e\u0433\u043e\u0434\u043d\u044b\u0435 \u043f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u044b \u0432\u0437\u044f\u0442\u044b \u043a\u0430\u043a \u043c\u0435\u0434\u0438\u0430\u043d\u044b \u0441\u0442\u0440\u043e\u043a \u0441 \u043c\u0435\u0442\u043a\u043e\u0439 \u043f\u044b\u043b\u044c\u043d\u043e\u0439 \u0431\u0443\u0440\u0438; "
        "\u0432\u0441\u0435 \u0441\u0435\u043a\u0442\u043e\u0440\u044b \u0432\u0435\u0442\u0440\u0430 \u0432\u0437\u0432\u0435\u0448\u0435\u043d\u044b \u043f\u043e \u0447\u0438\u0441\u043b\u0443 \u0442\u0430\u043a\u0438\u0445 \u043d\u0430\u0431\u043b\u044e\u0434\u0435\u043d\u0438\u0439."
        if automatic_weather else "\u0417\u0430\u0434\u0430\u043d \u043e\u0434\u0438\u043d \u0440\u0443\u0447\u043d\u043e\u0439 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 \u0438 \u043e\u0434\u043d\u043e \u043d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0432\u0435\u0442\u0440\u0430."
    )
    scenario_result["wind_sector_name"] = mode_name

    placement_reason = (
        f"\u041b\u0438\u043d\u0438\u0438 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u044b \u0441\u043d\u0430\u0440\u0443\u0436\u0438 \u043f\u043e\u043b\u044f, \u0441 \u043e\u0442\u0441\u0442\u0443\u043f\u043e\u043c {setback_m:.0f} \u043c \u043e\u0442 \u0435\u0433\u043e \u043d\u0430\u0432\u0435\u0442\u0440\u0435\u043d\u043d\u044b\u0445 \u0441\u0442\u043e\u0440\u043e\u043d, \u0447\u0442\u043e\u0431\u044b \u043d\u0435 \u0437\u0430\u043d\u0438\u043c\u0430\u0442\u044c \u043f\u0430\u0448\u043d\u044e. "
        "\u0421\u0442\u043e\u0440\u043e\u043d\u044b \u0432\u044b\u0431\u0438\u0440\u0430\u044e\u0442\u0441\u044f \u043f\u043e \u043d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f\u043c \u0432\u0435\u0442\u0440\u0430 \u0438\u0437 \u0430\u0440\u0445\u0438\u0432\u0430 \u043f\u044b\u043b\u044c\u043d\u044b\u0445 \u0431\u0443\u0440\u044c. "
        "\u0414\u043e\u0441\u0442\u0443\u043f\u043d\u043e\u0441\u0442\u044c \u0437\u0435\u043c\u043b\u0438 \u0437\u0430 \u0433\u0440\u0430\u043d\u0438\u0446\u0435\u0439 \u043a\u043e\u043d\u0442\u0443\u0440\u0430, \u0434\u043e\u0440\u043e\u0433\u0438 \u0438 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u044f \u043f\u043e\u0441\u0430\u0434\u043a\u0438 \u043d\u0443\u0436\u043d\u043e \u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u043e."
    )
    
    selected_axis_azimuth = selected_option["axis_azimuth_deg"]
    return {
        "area_ha": area_m2 / 10_000,
        "area_m2": area_m2,
        "station_distance_km": station_distance_km,
        "wind_from": float(mode_direction),
        "wind_to": (float(mode_direction) + 180) % 360,
        "scenario": scenario_result,
        "direction_counts": [
            {"wind_from": item["wind_from"], "sector_name": item["sector_name"], "count": item["event_count"]}
            for item in wind_scenarios
        ] if automatic_weather else [],
        "selection_mode": selection_mode,
        "selected_strips": selected_count,
        "selected_belt_ids": selected_belt_ids,
        "recommended_belt_ids": recommended_belt_ids,
        "available_belts": available_belts,
        "selection_tolerance_points": 5,
        "selection_reason": selection_reason,
        "planting_location": "outside_windward_perimeter",
        "setback_m": setback_m,
        "orientation": {
            "axis_azimuth_deg": selected_axis_azimuth,
            "placement_reason": placement_reason,
        },
        "baseline_risk": baseline_risk,
        "model_label": "сценарный индекс риска пыльной бури (Random Forest)",
        "options": result_options,
        "gis_method": {
            "layers": ["Copernicus DEM GLO-30", "ESA WorldCover 2021", "SoilGrids 250 m"],
            "purpose": "Предварительная проверка рельефа, земного покрова и почв вдоль внешних линий посадки.",
            "conflict_rule": "Линия помечается конфликтной, если вдоль неё найден хотя бы один пиксель ESA WorldCover класса застройки или постоянной воды.",
            "land_rights": "Класс WorldCover 40 может обозначать пашню, но кадастровые границы и сведения о собственнике не загружены; линию в таком покрове необходимо проверить по правам на землю.",
            "not_loaded": ["кадастровые границы участков", "дороги и полосы отвода", "границы аэропортов и приаэродромные ограничения"],
            "limits": "Уклон, ориентировочное направление вниз по склону и SoilGrids показываются как справочные признаки, но сейчас не влияют на выбор размещения и не являются признаками погодного Random Forest. WorldCover отдельно выявляет отмеченные на карте пиксели застройки и воды, но может пропускать узкие дороги и окрестности ВПП; он не заменяет кадастровые, дорожные и аэродромные ограничения. Слои не выбирают породу дерева. Направление потока не является полноценным гидрологическим моделированием.",
        },
        "model": {
            "name": model.get("name", "Random Forest"),
            "trees": len(model["trees"]),
            "features": model["features"],
        },
        "note": "Индекс показывает относительный результат сценарного прогона модели Элисты; это не калиброванная вероятность пыльной бури.",
    }


def geographic_xy(x: float, y: float, lon0: float, lat0: float) -> list[float]:
    lon_scale = math.cos(math.radians(lat0)) * math.pi / 180 * EARTH_RADIUS_M
    lat_scale = math.pi / 180 * EARTH_RADIUS_M
    return [lon0 + x / lon_scale, lat0 + y / lat_scale]


app = FastAPI(title="ПОЛЕ · ЛЕС API", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/api/health")
def health() -> dict[str, Any]:
    gis_status = gis_data_status()
    try:
        model = load_model()
    except ValueError as exc:
        return {"ok": False, "model": None, "trees": 0, "region": REGION_NAME, "message": str(exc), "gis": gis_status}
    return {
        "ok": True,
        "model": model.get("name"),
        "trees": len(model["trees"]),
        "region": REGION_NAME,
        "reference_scenario": model.get("reference_scenario"),
        "gis": gis_status,
    }


@app.post("/api/simulate", response_model=None)
async def simulate_endpoint(request: Request) -> JSONResponse | dict[str, Any]:
    try:
        body = await request.body()
        if not body or len(body) > 1_000_000:
            raise ValueError("Размер запроса должен быть меньше 1 МБ.")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Тело запроса должно быть JSON-объектом.")
        return await run_in_threadpool(simulate, payload)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:  # keep errors actionable in the local prototype
        return JSONResponse(status_code=500, content={"error": f"Ошибка расчёта: {exc}"})


def frontend_index() -> FileResponse:
    if not FRONTEND_INDEX.is_file():
        raise HTTPException(
            status_code=503,
            detail="Интерфейс ещё не собран. Выполните npm.cmd install и npm.cmd run build в папке frontend.",
        )
    return FileResponse(FRONTEND_INDEX)


@app.get("/", include_in_schema=False)
def frontend_root() -> FileResponse:
    return frontend_index()


app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets"), check_dir=False), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def frontend_fallback(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Эндпоинт не найден.")
    candidate = (FRONTEND_DIST / path).resolve()
    if FRONTEND_DIST.resolve() not in candidate.parents:
        raise HTTPException(status_code=404, detail="Файл не найден.")
    if candidate.is_file():
        return FileResponse(candidate)
    return frontend_index()


def main() -> None:
    import uvicorn

    host = os.environ.get("FOREST_HOST", "0.0.0.0")
    port = int(os.environ.get("FOREST_PORT", str(PORT)))
    print(f"API цифрового двойника запущен: http://127.0.0.1:{port}")
    if not MODEL_PATH.exists():
        print("Модель Элисты не найдена; сначала выполните tools/train_elista.py.")
    print("Остановите сервер сочетанием Ctrl+C.")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
