import logging
import sys
from pathlib import Path
from typing import List

import cv2
import numpy as np
import torch

from config import Config


class Detector:
    """YOLOv5 inference using the official repository."""

    def __init__(self, config: Config) -> None:
        det_cfg = config.get("detector") or {}
        model_path = det_cfg.get("model_path")
        repo_path = det_cfg.get("repo_path", "yolov5")
        self._conf_thres = det_cfg.get("confidence_threshold", 0.25)
        self._iou_thres = det_cfg.get("nms_threshold", 0.45)
        self._input_size = det_cfg.get("input_size", 640)

        self._log = logging.getLogger(self.__class__.__name__)

        repo = Path(repo_path)
        if not repo.exists():
            raise FileNotFoundError(f"YOLOv5 repo not found at {repo}")
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))

        from models.common import DetectMultiBackend  # type: ignore
        from utils.torch_utils import select_device  # type: ignore
        from utils.general import non_max_suppression, scale_boxes  # type: ignore

        self._nms = non_max_suppression
        self._scale = scale_boxes
        self._device = select_device("0" if torch.cuda.is_available() else "cpu")
        self._model = DetectMultiBackend(model_path, device=self._device)
        self._stride = int(self._model.stride.max())
        self._log.info(f"YOLOv5 model loaded from {model_path} on {self._device}")

    def _preprocess(self, frame: np.ndarray) -> torch.Tensor:
        img = cv2.resize(frame, (self._input_size, self._input_size))
        img = img[:, :, ::-1].transpose(2, 0, 1)  # BGR->RGB, HWC->CHW
        img = np.ascontiguousarray(img)
        img = torch.from_numpy(img).to(self._device)
        img = img.float() / 255.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
        return img

    def predict(self, frame: np.ndarray) -> List[List[int]]:
        """Return a list of [x, y, w, h] boxes for class 'car'."""
        if frame is None or frame.size == 0:
            return []

        img = self._preprocess(frame)
        pred = self._model(img, augment=False, visualize=False)
        pred = self._nms(pred, self._conf_thres, self._iou_thres)[0]

        boxes: List[List[int]] = []
        if pred is not None and len(pred):
            pred[:, :4] = self._scale(img.shape[2:], pred[:, :4], frame.shape).round()
            for *xyxy, conf, cls in pred:
                if int(cls) != 2 or float(conf) < self._conf_thres:
                    continue
                x1, y1, x2, y2 = map(int, xyxy)
                boxes.append([x1, y1, x2 - x1, y2 - y1])
        return boxes
