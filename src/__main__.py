# src/__main__.py

"""Entry point that emulates work without a controller.

The application connects to cameras defined in the config file and every
10 seconds runs object detection on one of two directions (main and side).
Results are logged to console and file.  When the detector or video capture
cannot be initialised, random numbers are reported instead.
"""

import logging
import random
import time

from .config import Config
from .logger import setup_logging
from .analyzer import CountsAggregator


def main() -> None:
    cfg = Config()
    setup_logging(cfg)
    log = logging.getLogger()

    log.info("Controller connection established (stub)")

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
    try:
        from .video_capture import VideoCapture

        vc = VideoCapture(cfg)
    except Exception as exc:  # pragma: no cover - depends on environment
        log.warning("Video capture unavailable, using random frames: %s", exc)

    # Map directions to camera ids
    directions = [
        ("main", ["1", "2"]),
        ("side", ["3"]),
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
                    if detector is not None and frame is not None:
                        count += len(detector.predict(frame))
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

