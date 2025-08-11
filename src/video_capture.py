import cv2
import yaml
import os
import logging
import numpy as np
from .config import Config

class VideoCapture:
    """
    Захват и маскирование кадров из RTSP-потоков или файлов.
    Маски хранятся в YAML-файлах `zone_<cam_id>.yaml` в директории mask_dir.
    Каждый файл должен содержать список зон со списком точек в поле `points`.
    """
    def __init__(self, config: Config):
        self._cams = config.get('cameras') or {}
        self._mask_dir = config.get('mask_dir')
        self._caps = {}
        self._masks = {}
        self._log = logging.getLogger(self.__class__.__name__)
        self._init_cameras()
        self._load_masks()

    def _init_cameras(self):
        for cam_id, uri in self._cams.items():
            cap = cv2.VideoCapture(uri)
            if not cap.isOpened():
                self._log.error(f"Cannot open camera {cam_id} ({uri})")
            self._caps[cam_id] = cap
            self._log.debug(f"Initialized VideoCapture for camera {cam_id}")

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

    def read(self, cam_id: str):
        """
        Вернуть следующий маскированный кадр для указанной камеры.
        """
        cap = self._caps.get(cam_id)
        if not cap:
            self._log.error(f"Camera {cam_id} not initialized")
            return None
        ret, frame = cap.read()
        if not ret:
            self._log.error(f"Failed to read from camera {cam_id}")
            return None
        mask = self._create_mask(frame.shape[:2], self._masks.get(cam_id, []))
        frame[mask == 0] = 0
        return frame

    def _create_mask(self, shape, polygons):
        h, w = shape
        mask = 255 * np.ones((h, w), dtype='uint8')
        for poly in polygons:
            pts = np.array(poly, dtype='int32')
            cv2.fillPoly(mask, [pts], 0)
        return mask
