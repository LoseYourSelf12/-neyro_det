import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, Tuple

import cv2

# ---------- цвета BGR для классов ----------
CLASS_COLOR = {
    "car": (0, 255, 0),  # зелёный
    "truck": (0, 165, 255),  # оранжевый
    "bus": (255, 0, 0),  # синий
}


def today_dir(root: Path) -> Path:
    return root / datetime.now().strftime("%Y%m%d")


def timestamp_name(ext: str = "jpg") -> str:
    return datetime.now().strftime("%H%M%S_%f")[:-3] + "." + ext


def unique_path(dir_: Path, fname: str) -> Path:
    p = dir_ / fname
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    k = 1
    while True:
        cand = dir_ / ("%s_%d%s" % (stem, k, suf))
        if not cand.exists():
            return cand
        k += 1


def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%d %s" % (n, unit)
        n //= 1024
    return "%d PB" % n


def is_saving_allowed(root: Path, min_free_gb: float, log: logging.Logger) -> bool:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        usage = shutil.disk_usage(root)
        return usage.free >= int(min_free_gb * (1024 ** 3))
    except Exception as e:
        log.error("Snapshots disabled: %s", e)
        return False


def space_ok(root: Path, min_free_gb: float) -> Tuple[bool, int]:
    try:
        usage = shutil.disk_usage(root)
        return (usage.free >= int(min_free_gb * (1024 ** 3))), usage.free
    except Exception:
        return False, 0


def save_frame(path: Path, img) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return bool(cv2.imwrite(str(path), img))
    except Exception:
        return False


def draw_detections(img, detections: Iterable[dict]):
    annotated = img.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.5
    thickness = 2

    for det in detections:
        try:
            if "xyxy" in det:
                x1, y1, x2, y2 = det["xyxy"]
            elif "bbox" in det:
                x1, y1, x2, y2 = det["bbox"]
            elif "xywh" in det:
                x, y, w, h = det["xywh"]
                x1, y1, x2, y2 = int(x - w / 2), int(y - h / 2), int(x + w / 2), int(y + h / 2)
            else:
                continue

            name = det.get("name", "obj")
            conf = det.get("conf")
            color = CLASS_COLOR.get(name, (0, 255, 255))

            cv2.rectangle(annotated, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
            label = "%s%s" % (name, (" %.2f" % float(conf)) if conf is not None else "")
            (tw, th), _ = cv2.getTextSize(label, font, font_scale, thickness)
            cv2.rectangle(annotated, (int(x1), int(y1) - th - 6), (int(x1) + tw + 4, int(y1)), color, -1)
            cv2.putText(annotated, label, (int(x1) + 2, int(y1) - 4),
                        font, font_scale, (255, 255, 255), 1, cv2.LINE_AA)
        except Exception:
            continue

    return annotated
