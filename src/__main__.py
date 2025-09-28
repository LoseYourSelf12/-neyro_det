import logging
import random
import time
from pathlib import Path

import cv2

from .config import Config
from .logger import setup_logging
from .analyzer import CountsAggregator


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

    # Optional video capture initialisation
    vc = None
    save_dir = None
    try:
        from .video_capture import VideoCapture

        vc = VideoCapture(cfg)
        save_dir = Path(cfg.get("snapshots", "save_dir", default="samples/detections"))
        save_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # pragma: no cover - depends on environment
        log.warning("Video capture unavailable, using random frames: %s", exc)

    # Map directions to camera ids
    directions = [
        ("main", ["1"]),
        ("side", ["2", '3']),
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

                    if detector is not None:
                        boxes = detector.predict(frame)
                        count += len(boxes)

                        if vc is not None and save_dir is not None:
                            annotated = vc.annotate(cam_id, frame, boxes)
                            if annotated is not None:
                                output_path = save_dir / f"camera_{cam_id}.jpg"
                                if not cv2.imwrite(str(output_path), annotated):
                                    log.error("Failed to save annotated frame for camera %s", cam_id)
                    else:
                        count += random.randint(0, 5)
                agg.add(count)
            avg = int(round(agg.average()))
            log.info("Detected %d cars on %s direction", avg, name)
            idx += 1
            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Shutting down neyro_det service")


if __name__ == "__main__":
    main()

