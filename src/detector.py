import logging
from typing import List

import cv2
import numpy as np
import torch
from ultralytics import YOLO

from config import Config

class Detector:
    """Инференс модели YOLO из библиотеки ultralytics."""

    def __init__(self, config: Config):
        model_path = config.get("detector", "model_path")
        self._conf_thres = config.get("detector", "confidence_threshold")
        self._input_size = config.get("detector", "input_size")

        self._log = logging.getLogger(self.__class__.__name__)

        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = YOLO(model_path)
        self._device = device
        self._log.info(f"YOLO model loaded from {model_path} on {device}")

    def predict(self, frame: np.ndarray) -> List[List[int]]:
        """Вернуть список прямоугольников [x, y, w, h] для класса 'car'."""

        if frame is None or frame.size == 0:
            return []

        results = self._model.predict(
            source=frame,
            device=self._device,
            imgsz=self._input_size,
            conf=self._conf_thres,
            verbose=False,
        )[0]

        boxes = []
        for box, cls, conf in zip(
            results.boxes.xyxy.cpu(),
            results.boxes.cls.cpu(),
            results.boxes.conf.cpu(),
        ):
            if int(cls) != 2 or float(conf) < self._conf_thres:
                continue
            x1, y1, x2, y2 = box.tolist()
            boxes.append([int(x1), int(y1), int(x2 - x1), int(y2 - y1)])

        return boxes
