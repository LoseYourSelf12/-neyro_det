import logging
import sys
from pathlib import Path
from typing import List

import cv2
import numpy as np
import torch

from .config import Config


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
        from utils.general import non_max_suppression, scale_coords  # type: ignore
        from utils.augmentations import letterbox  # <-- ВАЖНО

        self._letterbox = letterbox
        self._nms = non_max_suppression
        self._scale = scale_coords
        self._device = select_device("0" if torch.cuda.is_available() else "cpu")
        self._model = DetectMultiBackend(model_path, device=self._device)
        stride = self._model.stride
        if hasattr(stride, "max"):
            stride = stride.max()
        elif isinstance(stride, (list, tuple)):
            stride = max(stride)
        self._stride = int(stride)
        self._log.info(f"YOLOv5 model loaded from {model_path} on {self._device}")

    def _preprocess(self, frame: np.ndarray) -> torch.Tensor:
        # Letterbox: сохраняем соотношение сторон + паддинг
        # new_shape можно передать числом или кортежем
        img = self._letterbox(frame, (self._input_size, self._input_size),
                              stride=self._stride, auto=True)[0]
        # BGR->RGB, HWC->CHW
        img = img[:, :, ::-1].transpose(2, 0, 1)
        img = np.ascontiguousarray(img)
        img = torch.from_numpy(img).to(self._device)
        img = img.float() / 255.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
        return img

    def predict(self, frame: np.ndarray):
        """
        Возвращает список детекций:
        [{'xyxy': (x1,y1,x2,y2), 'cls': int, 'conf': float, 'name': str}, ...]
        Оставляем только классы: car(2), bus(5), truck(7) по COCO.
        """
        if frame is None or frame.size == 0:
            return []

        im0 = frame
        img = self._preprocess(im0)
        pred = self._model(img, augment=False, visualize=False)
        pred = self._nms(pred, self._conf_thres, self._iou_thres)[0]

        keep_cls = {2: "car", 5: "bus", 7: "truck"}
        out = []
        if pred is not None and len(pred):
            # обратно из letterbox в размер исходного кадра
            pred[:, :4] = self._scale(img.shape[2:], pred[:, :4], im0.shape).round()

            H, W = im0.shape[:2]
            for *xyxy, conf, cls in pred:
                cls_i = int(cls)
                if cls_i not in keep_cls:
                    continue
                c = float(conf)
                x1, y1, x2, y2 = map(int, xyxy)

                # клип в границы
                x1 = max(0, min(x1, W - 1))
                y1 = max(0, min(y1, H - 1))
                x2 = max(0, min(x2, W - 1))
                y2 = max(0, min(y2, H - 1))
                if x2 <= x1 or y2 <= y1:
                    continue

                out.append({
                    "xyxy": (x1, y1, x2, y2),
                    "cls": cls_i,
                    "conf": c,
                    "name": keep_cls[cls_i],
                })
        return out
