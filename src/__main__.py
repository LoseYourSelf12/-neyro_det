import logging
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
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    rid = uuid.uuid4().hex[:8]
    return f"img_cam{cam_id}_{stamp}_{rid}.{ext}"


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
        # фон под текст
        cv2.rectangle(annotated, (x1, y1 - th - 6), (x1 + tw + 4, y1), color, -1)
        # текст контрастным цветом (чёрный/белый в зависимости от яркости фона можно не усложнять)
        cv2.putText(annotated, label, (x1 + 2, y1 - 4), font, font_scale, (255, 255, 255), 1, cv2.LINE_AA)

    return annotated


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
    save_root = None
    try:
        from .video_capture import VideoCapture
        vc = VideoCapture(cfg)
        save_root = Path(cfg.get("snapshots", "save_dir", default="samples/detections"))
        save_root.mkdir(parents=True, exist_ok=True)
        log.info("Snapshots will be saved under: %s", save_root.resolve())
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

            for _ in range(shots):
                count = 0
                for cam_id in cam_ids:
                    frame = vc.read(cam_id) if vc is not None else None
                    if frame is None:
                        log.warning("Frame for camera %s is unavailable, skipping detection", cam_id)
                        continue

                    out_img = frame
                    if detector is not None:
                        dets = detector.predict(frame)  # теперь detailed
                        count += sum(1 for d in dets if d["name"] in ("car", "bus", "truck"))
                        out_img = _draw_detections(frame, dets)

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
