"""Train and export the Elista dust-storm Random Forest."""

from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import openpyxl
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, precision_score, recall_score, roc_auc_score


ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "model" / "elista_forest.json"
FEATURE_NAMES = [
    "temperature_c",
    "humidity_pct",
    "wind_speed_ms",
    "gust_speed_ms",
    "wind_east_ms",
    "wind_north_ms",
    "visibility_km",
    "month_sin",
    "month_cos",
]


def one_input(pattern: str) -> Path:
    matches = [Path(path) for path in glob.glob(str(ROOT / pattern))]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one input matching {pattern!r}; found {len(matches)}.")
    return matches[0]


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_visibility(value: Any) -> float | None:
    text = str(value or "").strip().lower().replace(",", ".")
    if not text:
        return None
    if text.startswith("менее"):
        return 0.025
    if "-" in text:
        try:
            low, high = (float(part.strip()) for part in text.split("-", 1))
        except ValueError:
            return None
        # RP5 gives sub-kilometre visibility intervals in metres.
        return (low + high) / 2000 if max(low, high) > 50 else (low + high) / 2
    return to_float(text)


def read_rp5(path: Path) -> dict[dt.datetime, dict[str, str]]:
    observations: dict[dt.datetime, dict[str, str]] = {}
    with path.open("r", encoding="cp1251", newline="") as stream:
        reader = csv.reader(stream, delimiter=";")
        header: list[str] | None = None
        for row in reader:
            if row and not row[0].lstrip().startswith("#"):
                header = row
                break
        if header is None:
            raise RuntimeError("RP5 CSV header was not found.")
        indexes = {name: header.index(name) for name in ("T", "U", "Ff", "ff10", "ff3", "VV")}
        time_index = header.index("Местное время в Элисте")
        for row in reader:
            if not row or len(row) <= max(indexes.values()):
                continue
            try:
                timestamp = dt.datetime.strptime(row[time_index].strip(), "%d.%m.%Y %H:%M")
            except (IndexError, ValueError):
                continue
            observations[timestamp] = {name: row[index].strip() for name, index in indexes.items()}
    return observations


def load_examples(meteo_path: Path, rp5_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    rp5 = read_rp5(rp5_path)
    workbook = openpyxl.load_workbook(meteo_path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    header = next(rows)
    expected_headers = (
        "Номер", "Год", "Месяц Грин", "День Грин", "Срок Грин", "Месяц", "День",
        "Срок Мес", "Время мес", "Погода между сроками", "Погода в срок", "Напр.ветра",
        "Скорость", "Макс.скор", "Давление на стан.",
    )
    if tuple(header) != expected_headers:
        raise RuntimeError("The meteo.ru workbook columns changed; check docs/DATA_AUDIT.md before training.")

    features: list[list[float]] = []
    labels: list[int] = []
    years: list[int] = []
    skipped = {"no_rp5_timestamp": 0, "invalid_code": 0, "missing_weather_features": 0}

    for row in rows:
        try:
            timestamp = dt.datetime(int(row[1]), int(row[2]), int(row[3]), int(row[4]))
        except (TypeError, ValueError):
            skipped["invalid_code"] += 1
            continue
        code = to_float(row[10])
        if code is None or code < 0 or code > 99 or not code.is_integer():
            skipped["invalid_code"] += 1
            continue
        rp5_row = rp5.get(timestamp)
        if rp5_row is None:
            skipped["no_rp5_timestamp"] += 1
            continue

        temperature = to_float(rp5_row["T"])
        humidity = to_float(rp5_row["U"])
        wind_speed = to_float(row[12])
        wind_from = to_float(row[11])
        if None in (temperature, humidity, wind_speed, wind_from) or wind_from == 999:
            skipped["missing_weather_features"] += 1
            continue

        gust_candidates = [wind_speed, to_float(row[13])]
        gust_candidates.extend(to_float(rp5_row[name]) for name in ("Ff", "ff10", "ff3"))
        gust_speed = max(value for value in gust_candidates if value is not None)

        visibility = parse_visibility(rp5_row["VV"])
        # Only 300 rows have a blank RP5 visibility entry; 10 km is the most frequent report.
        if visibility is None:
            visibility = 10.0

        wind_to = math.radians((wind_from + 180) % 360)
        month_angle = 2 * math.pi * (timestamp.month - 1) / 12
        features.append([
            temperature,
            humidity,
            wind_speed,
            gust_speed,
            wind_speed * math.sin(wind_to),
            wind_speed * math.cos(wind_to),
            visibility,
            math.sin(month_angle),
            math.cos(month_angle),
        ])
        # Per the user's instruction, only meteo.ru codes 30-35 are positive labels.
        labels.append(int(30 <= code <= 35))
        years.append(timestamp.year)

    workbook.close()
    X = np.asarray(features, dtype=np.float64)
    y = np.asarray(labels, dtype=np.int8)
    year_array = np.asarray(years, dtype=np.int16)
    if X.ndim != 2 or X.shape[1] != len(FEATURE_NAMES) or len(np.unique(y)) != 2:
        raise RuntimeError("Not enough valid positive and negative examples to train the classifier.")
    return X, y, year_array, {"skipped_rows": skipped, "meteo_rows": sheet.max_row - 1, "rp5_rows": len(rp5)}


def new_forest() -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=400,
        max_depth=8,
        min_samples_leaf=4,
        max_features="sqrt",
        class_weight="balanced_subsample",
        random_state=42,
        n_jobs=-1,
    )


def reference_scenario(X: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """Summarize the labelled Elista dust-storm rows for a default user scenario."""
    positives = X[y == 1]
    if not len(positives):
        raise RuntimeError("Cannot build a reference scenario without positive dust-storm rows.")
    month_angles = np.mod(np.arctan2(positives[:, 7], positives[:, 8]), 2 * math.pi)
    months = (np.rint(month_angles * 12 / (2 * math.pi)).astype(int) % 12) + 1
    month = Counter(months.tolist()).most_common(1)[0][0]
    wind_to = np.mod(np.degrees(np.arctan2(positives[:, 4], positives[:, 5])), 360)
    wind_from = np.mod(wind_to + 180, 360)
    sectors = (np.floor(np.mod(wind_from + 22.5, 360) / 45).astype(int) % 8)
    sector_counts = Counter(sectors.tolist())
    sector = sector_counts.most_common(1)[0][0]
    direction_names = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
    return {
        "temperature_c": float(np.median(positives[:, 0])),
        "humidity": float(np.median(positives[:, 1])),
        "wind_speed": float(np.median(positives[:, 2])),
        "wind_gust": float(np.median(positives[:, 3])),
        "wind_from": float(sector * 45),
        "wind_sector_name": direction_names[sector],
        "visibility": float(np.median(positives[:, 6])),
        "month": int(month),
        "event_count": int(len(positives)),
        "direction_counts": [
            {"wind_from": int(sector_id * 45), "sector_name": direction_names[sector_id], "count": int(count)}
            for sector_id, count in sector_counts.most_common()
        ],
        "period": "2010–2024",
        "basis": "Медианы температуры, влажности, скорости ветра, порывов и видимости по отмеченным эпизодам; месяц и сектор ветра — самые частые среди них.",
    }


def export_tree(estimator: Any, positive_class_index: int) -> dict[str, Any]:
    tree = estimator.tree_
    values = tree.value[:, 0, :]
    totals = values.sum(axis=1)
    probabilities = np.divide(values[:, positive_class_index], totals, out=np.zeros_like(totals), where=totals > 0)
    return {
        "left": tree.children_left.astype(int).tolist(),
        "right": tree.children_right.astype(int).tolist(),
        "feature": tree.feature.astype(int).tolist(),
        "threshold": tree.threshold.astype(float).tolist(),
        "values": [[float(1 - p), float(p)] for p in probabilities],
    }


def main() -> None:
    meteo_path = one_input("*meteo.ru.xlsx")
    rp5_path = one_input("*rp5.ru.csv")
    X, y, years, load_stats = load_examples(meteo_path, rp5_path)
    default_scenario = reference_scenario(X, y)

    training_mask = years <= 2021
    holdout_mask = years >= 2022
    if y[training_mask].sum() == 0 or y[holdout_mask].sum() == 0:
        raise RuntimeError("Temporal holdout needs positive dust-storm labels in both periods.")
    validation_model = new_forest().fit(X[training_mask], y[training_mask])
    holdout_scores = validation_model.predict_proba(X[holdout_mask])[:, list(validation_model.classes_).index(1)]
    validation = {
        "training_period": "2010-2021",
        "holdout_period": "2022-2024",
        "training_rows": int(training_mask.sum()),
        "holdout_rows": int(holdout_mask.sum()),
        "training_positive_rows": int(y[training_mask].sum()),
        "holdout_positive_rows": int(y[holdout_mask].sum()),
        "holdout_positive_rate": float(y[holdout_mask].mean()),
        "average_precision": float(average_precision_score(y[holdout_mask], holdout_scores)),
        "roc_auc": float(roc_auc_score(y[holdout_mask], holdout_scores)),
        "precision_at_score_0_5": float(precision_score(y[holdout_mask], holdout_scores >= 0.5, zero_division=0)),
        "recall_at_score_0_5": float(recall_score(y[holdout_mask], holdout_scores >= 0.5, zero_division=0)),
    }

    final_model = new_forest().fit(X, y)
    positive_class_index = list(final_model.classes_).index(1)
    artifact = {
        "schema_version": 1,
        "name": "Random Forest Элиста · пыльные бури",
        "features": FEATURE_NAMES,
        "reference_scenario": default_scenario,
        "trees": [export_tree(estimator, positive_class_index) for estimator in final_model.estimators_],
        "training": {
            "station_wmo": 34861,
            "period": "2010-2024",
            "label_codes": "meteo.ru: Погода в срок = 30..35; code 9 excluded",
            "meteo_file": meteo_path.name,
            "rp5_file": rp5_path.name,
            "rows": int(len(y)),
            "positive_rows": int(y.sum()),
            "negative_rows": int(len(y) - y.sum()),
            "skipped_rows": load_stats["skipped_rows"],
            "validation": validation,
            "classifier": {
                "type": "sklearn.ensemble.RandomForestClassifier",
                "n_estimators": 400,
                "max_depth": 8,
                "min_samples_leaf": 4,
                "class_weight": "balanced_subsample",
                "random_state": 42,
            },
            "score_note": "Uncalibrated Random Forest score; use for relative scenario comparison, not as event probability.",
        },
    }
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(json.dumps(artifact, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Saved {MODEL_PATH.relative_to(ROOT)}")
    print(json.dumps({"rows": len(y), "positive_rows": int(y.sum()), "validation": validation}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
