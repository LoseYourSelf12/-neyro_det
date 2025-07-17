import cv2
import numpy as np
import logging
from config import Config

# Попытаемся импортировать onnxruntime
try:
    import onnxruntime as ort
except ImportError:
    ort = None

class Detector:
    """
    Инференс ONNX-модели YOLOv5 для подсчёта машин на кадре.
    Использует сначала OpenCV DNN, а при ошибке – onnxruntime.
    """
    def __init__(self, config: Config):
        model_path = config.get('detector', 'model_path')
        self._input_size = config.get('detector', 'input_size')
        self._conf_thres = config.get('detector', 'confidence_threshold')
        self._nms_thres = config.get('detector', 'nms_threshold')
        self._log = logging.getLogger(self.__class__.__name__)

        # Пытаемся загрузить через OpenCV DNN
        try:
            self._net = cv2.dnn.readNetFromONNX(model_path)
            self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
            self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
            self._using_ort = False
            self._log.info("YOLOv5 загружен через OpenCV DNN (CUDA).")
        except cv2.error as e:
            self._log.warning(f"OpenCV DNN не смог импортировать ONNX ({e}).")
            if ort is None:
                raise RuntimeError("Требуется onnxruntime для fallback-инференса.")
            # Загрузка через onnxruntime
            self._session = ort.InferenceSession(model_path, providers=['CPUExecutionProvider'])
            self._input_name = self._session.get_inputs()[0].name
            self._using_ort = True
            self._log.info("YOLOv5 загружен через onnxruntime (CPU).")

    def predict(self, frame):
        if frame is None or frame.size == 0:
            return []
        # Подготовка входа
        blob = cv2.dnn.blobFromImage(
            frame, 1/255.0,
            (self._input_size, self._input_size),
            swapRB=True, crop=False
        )
        if not self._using_ort:
            self._net.setInput(blob)
            preds = self._net.forward()
        else:
            # onnxruntime: blob shape = [1,3,H,W], но нужен [1,H,W,3] или [1,3,H,W]?
            # В YOLOv5 ONNX вход — [1,3,H,W]
            input_blob = blob
            preds = self._session.run(None, {self._input_name: input_blob})[0]

        return self._postprocess(preds, frame.shape[:2])

    def _postprocess(self, preds, shape):
        h_frame, w_frame = shape
        preds = preds.reshape(-1, preds.shape[-1])
        boxes, confidences = [], []
        for det in preds:
            conf = float(det[4])
            if conf < self._conf_thres:
                continue
            scores = det[5:]
            class_id = int(np.argmax(scores))
            score = float(scores[class_id])
            # Только класс «car» (2)
            if score < self._conf_thres or class_id != 2:
                continue
            cx, cy, w, h = det[0:4]
            x = int((cx - w/2) * w_frame)
            y = int((cy - h/2) * h_frame)
            ww = int(w * w_frame)
            hh = int(h * h_frame)
            boxes.append([x, y, ww, hh])
            confidences.append(score)
        idxs = cv2.dnn.NMSBoxes(boxes, confidences, self._conf_thres, self._nms_thres)
        if len(idxs) == 0:
            return []
        # развернём индексы в плоский список
        flat = [i[0] if isinstance(i, (list, tuple, np.ndarray)) else i for i in idxs]
        return [boxes[i] for i in flat]