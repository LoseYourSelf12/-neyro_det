import os
import logging
from typing import Dict, List, Optional

import cv2
import numpy as np
import requests
import yaml

from .config import Config

class VideoCapture:
    """
    Захват и маскирование кадров по HTTP-снимкам.
    Маски хранятся в YAML-файлах `zone_<cam_id>.yaml` в директории mask_dir.
    Каждый файл должен содержать список зон со списком точек в поле `points`.
    """
    def __init__(self, config: Config):
        self._cams = config.get('cameras') or {}
        self._mask_dir = config.get('mask_dir')
        self._session = requests.Session()
        self._timeout = config.get('cameras_timeout', default=5) or 5
        self._masks: Dict[str, List[List[int]]] = {}
        self._log = logging.getLogger(self.__class__.__name__)
        self._init_cameras()
        self._load_masks()

    def _init_cameras(self):
        normalized: Dict[str, str] = {}
        for cam_id, data in self._cams.items():
            uri = None
            if isinstance(data, dict):
                uri = data.get("snapshot") or data.get("snapshot_url") or data.get("url")
            elif isinstance(data, str):
                uri = data

            if not uri:
                self._log.error(f"Snapshot URL for camera {cam_id} is not provided")
                continue

            normalized[cam_id] = uri
            self._log.debug(f"Registered snapshot URL for camera {cam_id}")

        self._cams = normalized

    def _load_masks(self):
        for cam_id in self._cams:
            path = os.path.join(self._mask_dir, f"zone_{cam_id}.yaml")
            if os.path.isfile(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = yaml.safe_load(f)
                    polygons = []
                    if isinstance(data, dict):
                        if 'zones' in data and isinstance(data['zones'], list):
                            for zone in data['zones']:
                                pts = zone.get('points', [])
                                if pts:
                                    polygons.append(pts)
                        elif 'points' in data:
                            polygons.append(data.get('points', []))
                    self._masks[cam_id] = polygons
                    self._log.debug(f"Loaded mask for cam {cam_id}")
                except Exception as e:
                    self._masks[cam_id] = []
                    self._log.error(f"Failed to load mask for cam {cam_id}: {e}")
            else:
                self._masks[cam_id] = []
                self._log.warning(f"Mask file not found for cam {cam_id}, no masking applied.")

    def read(self, cam_id: str) -> Optional[np.ndarray]:
        """
        Вернуть текущий маскированный кадр для указанной камеры.
        """
        uri = self._cams.get(cam_id)
        if not uri:
            self._log.error(f"Camera {cam_id} not initialized")
            return None

        try:
            response = self._session.get(uri, timeout=self._timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            self._log.error(f"Failed to fetch snapshot from camera {cam_id}: {exc}")
            return None

        frame = cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            self._log.error(f"Failed to decode snapshot from camera {cam_id}")
            return None

        mask = self._create_mask(frame.shape[:2], self._masks.get(cam_id, []))
        # frame[mask == 0] = 0
        frame = cv2.bitwise_and(frame, frame, mask=mask)
        return frame

    def _create_mask(self, shape, polygons):
        h, w = shape
        # mask = 255 * np.ones((h, w), dtype='uint8')
        mask = np.zeros((h, w), dtype='uint8')
        for poly in polygons:
            pts = np.array(poly, dtype='int32')
            # cv2.fillPoly(mask, [pts], 0)
            cv2.fillPoly(mask, [pts], 255)
        return mask

    def annotate(self, cam_id: str, frame: np.ndarray, boxes: List[List[int]]) -> Optional[np.ndarray]:
        """
        Наложить маски и боксы на кадр.
        Возвращает аннотированный кадр или None, если входные данные некорректны.
        """
        if frame is None or frame.size == 0:
            return None

        annotated = frame.copy()
        polygons = self._masks.get(cam_id, [])
        if polygons:
            overlay = annotated.copy()
            for poly in polygons:
                pts = np.array(poly, dtype=np.int32)
                cv2.fillPoly(overlay, [pts], (0, 255, 0))
                cv2.polylines(overlay, [pts], isClosed=True, color=(0, 200, 0), thickness=2)
            cv2.addWeighted(overlay, 0.3, annotated, 0.7, 0, annotated)

        for x, y, w, h in boxes:
            cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 0, 255), 2)

        return annotated
