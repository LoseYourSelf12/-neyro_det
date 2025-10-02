import logging
import random
import time
from pathlib import Path
import traceback
from datetime import datetime
import uuid

import cv2

from .config import Config
from .logger import setup_logging
from .analyzer import CountsAggregator


def _unique_name(cam_id: str, ext: str = "jpg") -> str:
    now = datetime.now()
    # миллисекунды + короткий uuid, чтобы исключить коллизии при быстром сохранении
    stamp = now.strftime("%Y%m%d_%H%M%S_%f")[:-3]  # YYYYMMDD_HHMMSS_mmm
    rid = uuid.uuid4().hex[:8]
    return f"img_cam{cam_id}_{stamp}_{rid}.{ext}"


def main() -> None:
    cfg = Config()
    setup_logging(cfg)
    log = logging.getLogger()

    log.info("Controller connection established")

    # Optional detector initialisation
    detector = None
    try:
        from .detector import Detector  # heavy import

        detector = Detector(cfg)
        log.info("Detector model loaded")
    except Exception as exc:  # pragma: no cover - depends on environment
        log.warning("Detector unavailable, using random counts: %s", exc)
        traceback.print_exc()

    # Optional video capture initialisation
    vc = None
    save_root = None
    try:
        from .video_capture import VideoCapture

        vc = VideoCapture(cfg)
        save_root = Path(cfg.get("snapshots", "save_dir", default="samples/detections"))
        save_root.mkdir(parents=True, exist_ok=True)
        log.info("Snapshots will be saved under: %s", save_root.resolve())
    except Exception as exc:  # pragma: no cover - depends on environment
        log.warning("Video capture unavailable, using random frames: %s", exc)

    # Map directions to camera ids
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
            for _ in range(shots):
                count = 0
                for cam_id in cam_ids:
                    frame = vc.read(cam_id) if vc is not None else None
                    if frame is None:
                        log.warning("Frame for camera %s is unavailable, skipping detection", cam_id)
                        continue

                    # детекция (если доступна)
                    boxes = []
                    if detector is not None:
                        boxes = detector.predict(frame)
                        count += len(boxes)
                        annotated = vc.annotate(cam_id, frame, boxes)
                        out_img = annotated if annotated is not None else frame
                    else:
                        # без детектора — просто сохраняем чистый кадр
                        out_img = frame

                    # сохранение
                    if save_root is not None:
                        cam_dir = save_root / f"cam_{cam_id}"
                        cam_dir.mkdir(parents=True, exist_ok=True)
                        fname = _unique_name(cam_id, ext="jpg")
                        out_path = cam_dir / fname
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
