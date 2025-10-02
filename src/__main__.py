import logging
import time
from pathlib import Path
import traceback
from datetime import datetime
import shutil

import cv2

from .config import Config
from .logger import setup_logging
from .analyzer import CountsAggregator


# Цвета BGR для классов
CLASS_COLOR = {
    "car":   (0, 255, 0),     # зелёный
    "truck": (0, 165, 255),   # оранжевый
    "bus":   (255, 0, 0),     # синий
}

def _draw_detections(img, detections):
    """
    detections: [{'xyxy':(x1,y1,x2,y2),'name':str,'conf':float,'cls':int}, ...]
    """
    annotated = img.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.5
    thickness = 2

    for det in detections:
        x1, y1, x2, y2 = det["xyxy"]
        name = det.get("name", "obj")
        conf = det.get("conf", None)
        color = CLASS_COLOR.get(name, (0, 255, 255))  # default — жёлтый

        # bbox
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)

        # метка
        label = f"{name}" + (f" {conf:.2f}" if conf is not None else "")
        (tw, th), _ = cv2.getTextSize(label, font, font_scale, thickness)
        cv2.rectangle(annotated, (x1, y1 - th - 6), (x1 + tw + 4, y1), color, -1)
        cv2.putText(annotated, label, (x1 + 2, y1 - 4), font, font_scale, (255, 255, 255), 1, cv2.LINE_AA)

    return annotated


def _today_dir(root: Path) -> Path:
    """<root>/YYYYMMDD"""
    return root / datetime.now().strftime("%Y%m%d")


def _timestamp_name(ext: str = "jpg") -> str:
    """HHMMSS_mmm.jpg"""
    return datetime.now().strftime("%H%M%S_%f")[:-3] + f".{ext}"


def _unique_path(dir_: Path, fname: str) -> Path:
    """Если fname занят — добавляем _1, _2, ..."""
    p = dir_ / fname
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    k = 1
    while True:
        cand = dir_ / f"{stem}_{k}{suf}"
        if not cand.exists():
            return cand
        k += 1


def _assert_writable(path: Path, log: logging.Logger) -> bool:
    """
    Проверяем, что можно создать и удалить файл в целевой директории.
    НЕ используем альтернативы — при провале возвращаем False.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception as e:
        log.error("Snapshots DISABLED: save dir is not writable: %s (%s)", path, e)
        return False


def _has_free_space(path: Path, min_free_bytes: int, log: logging.Logger) -> bool:
    """Порог по свободному месту в разделе."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free >= min_free_bytes
    except Exception as e:
        log.error("Snapshots DISABLED: cannot read disk usage for %s (%s)", path, e)
        return False


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} PB"


def main() -> None:
    cfg = Config()
    setup_logging(cfg)
    log = logging.getLogger()

    log.info("Controller connection established")

    # Detector
    detector = None
    try:
        from .detector import Detector
        detector = Detector(cfg)
        log.info("Detector model loaded")
    except Exception as exc:  # pragma: no cover
        log.warning("Detector unavailable, using random counts: %s", exc)
        traceback.print_exc()

    # Video capture
    vc = None
    save_root: Path | None = None
    try:
        from .video_capture import VideoCapture
        vc = VideoCapture(cfg)

        # Путь из конфига (например, /media/MYUSB/neyro_det)
        candidate_root = Path(cfg.get("snapshots", "save_dir", default="/media/MYUSB/neyro_det"))

        # Порог свободного места (по умолчанию 1.0 GB)
        min_free_gb = float(cfg.get("snapshots", "min_free_gb", default=1.0))
        min_free_bytes = int(min_free_gb * (1024**3))

        # Проверка записи и свободного места НА СТАРТЕ
        if _assert_writable(candidate_root, log) and _has_free_space(candidate_root, min_free_bytes, log):
            save_root = candidate_root
            free = shutil.disk_usage(save_root).free
            log.info(
                "Snapshots ENABLED at: %s (free: %s, threshold: %.2f GB)",
                save_root.resolve(), _fmt_bytes(free), min_free_gb
            )
        else:
            log.error("Snapshots DISABLED: saving will be skipped (no fallback).")

    except Exception as exc:  # pragma: no cover
        log.warning("Video capture unavailable, using random frames: %s", exc)

    # Камеры по направлениям
    directions = [
        ("main", ["1"]),
        ("side", ["2", "3"]),
    ]

    shots = cfg.get("analysis", "shots_per_phase", default=1)

    idx = 0
    try:
        while True:
            name, cam_ids = directions[idx % len(directions)]
            agg = CountsAggregator()

            # Ежецикловая проверка свободного места (если сохранение вообще включено)
            saving_allowed = False
            if save_root is not None:
                min_free_gb = float(cfg.get("snapshots", "min_free_gb", default=1.0))
                min_free_bytes = int(min_free_gb * (1024**3))
                if _has_free_space(save_root, min_free_bytes, log):
                    saving_allowed = True
                else:
                    free = shutil.disk_usage(save_root).free
                    log.warning(
                        "Snapshots PAUSED: low disk space at %s (free: %s < %.2f GB).",
                        save_root.resolve(), _fmt_bytes(free), min_free_gb
                    )

            # Папка за сегодня (если сохраняем)
            day_dir = _today_dir(save_root) if (save_root is not None and saving_allowed) else None
            if day_dir is not None:
                day_dir.mkdir(parents=True, exist_ok=True)

            for _ in range(shots):
                count = 0
                for cam_id in cam_ids:
                    frame = vc.read(cam_id) if vc is not None else None
                    if frame is None:
                        log.warning("Frame for camera %s is unavailable, skipping detection", cam_id)
                        continue

                    out_img = frame
                    if detector is not None:
                        dets = detector.predict(frame)  # [{'xyxy':..., 'name':..., 'conf':...}, ...]
                        count += sum(1 for d in dets if d["name"] in ("car", "bus", "truck"))
                        out_img = _draw_detections(frame, dets)

                    # сохранение (только если разрешено)
                    if day_dir is not None:
                        cam_dir = day_dir / f"cam_{cam_id}"
                        cam_dir.mkdir(parents=True, exist_ok=True)

                        fname = _timestamp_name(ext="jpg")
                        out_path = _unique_path(cam_dir, fname)
                        ok = cv2.imwrite(str(out_path), out_img)
                        if ok:
                            log.info("Saved snapshot for camera %s: %s", cam_id, out_path.resolve())
                        else:
                            log.error("Failed to save frame for camera %s to %s", cam_id, out_path)

                agg.add(count)

            avg = int(round(agg.average()))
            log.info("Detected %d cars on %s direction", avg, name)
            idx += 1
            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Shutting down neyro_det service")


if __name__ == "__main__":
    main()
