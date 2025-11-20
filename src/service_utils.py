import logging
from pathlib import Path
from typing import Iterable

from logging.handlers import RotatingFileHandler

from .analyzer import CountsAggregator
from .snapshots import draw_detections, save_frame, timestamp_name, unique_path


def setup_progchange_logger(cfg) -> logging.Logger:
    path = cfg.get("logging", "program_changes_file", default="logs/program_changes.log")
    level_name = cfg.get("logging", "level", default="INFO")
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    max_bytes = cfg.get("logging", "max_bytes", default=10_485_760)
    backup_count = cfg.get("logging", "backup_count", default=5)

    logger = logging.getLogger("progchange")
    logger.setLevel(level)
    logger.propagate = False  # чтобы не дублировать в общий лог

    if not logger.handlers:
        log_dir = Path(path).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=int(max_bytes), backupCount=int(backup_count), encoding="utf-8")
        # CSV: timestamp,FROM,TO
        fmt = logging.Formatter("%(asctime)s,%(message)s", datefmt="%Y-%m-%d %H:%M:%S.%f")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        logger.addHandler(fh)
    return logger


def count_direction(detector, vc, cam_ids: Iterable[str], shots: int, log: logging.Logger, save_dir: Path):
    agg = CountsAggregator()
    for _ in range(max(1, int(shots))):
        total = 0
        for cam_id in cam_ids:
            frame = vc.read(cam_id)
            if frame is None:
                log.warning("Frame for camera %s is unavailable, skipping detection", cam_id)
                continue

            dets = detector.predict(frame) if detector else []
            count = sum(1 for d in dets if d.get("name") in ("car", "bus", "truck"))
            total += count

            out_img = draw_detections(frame, dets) if dets else frame

            if save_dir is not None:
                cam_dir = save_dir / ("cam_%s" % cam_id)
                cam_dir.mkdir(parents=True, exist_ok=True)
                fname = timestamp_name("jpg")
                out_path = unique_path(cam_dir, fname)
                if save_frame(out_path, out_img):
                    logging.getLogger("service").info("Saved snapshot for camera %s: %s", cam_id, out_path.resolve())
                else:
                    logging.getLogger("service").error("Failed to save frame for camera %s to %s", cam_id, out_path)

        agg.add(total)
    return int(round(agg.average()))
