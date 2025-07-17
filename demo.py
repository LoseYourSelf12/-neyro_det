import sys
import cv2
import yaml
import numpy as np
import threading
import time
from queue import Queue
from ultralytics import YOLO
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QHBoxLayout, QVBoxLayout, QGridLayout, QListWidget
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QPixmap, QImage, QPainter, QColor, QFont

# Constants
VIDEO_PATHS = ['samples/test_vid.mp4', 'samples/test_vid.mp4']
ZONE_FILES = ['masks/zone_1.yaml', 'masks/zone_2.yaml', 'masks/zone_3.yaml', 'masks/zone_4.yaml']
DETECT_CLASSES = [2, 5, 7]  # COCO IDs: 2-car, 5-bus, 7-truck
REGION_NAMES = ['Cam1|Zone1', 'Cam2|Zone1', 'Cam1|Zone2', 'Cam2|Zone2']

class MaskLoader:
    def __init__(self, file_path, frame_shape):
        # Load YAML and extract points list
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
        # Support two formats: top‑level 'points' or nested under 'zones'
        if isinstance(data, dict) and 'points' in data:
            pts_rel = data['points']
        elif isinstance(data, dict) and 'zones' in data and isinstance(data['zones'], list) and data['zones']:
            zone_entry = data['zones'][0]
            pts_rel = zone_entry.get('points')
        else:
            raise ValueError(f"Zone file '{file_path}' does not contain 'points' or 'zones' with points.")
        if not isinstance(pts_rel, list) or len(pts_rel) < 3:
            raise ValueError(f"Zone file '{file_path}' must contain at least 3 points.")
        h, w = frame_shape[:2]
        abs_pts = []
        for pt in pts_rel:
            if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
                raise ValueError(f"Invalid point format {pt} in '{file_path}', expected [x, y].")
            x_rel, y_rel = pt
            if not (isinstance(x_rel, (int, float)) and isinstance(y_rel, (int, float))):
                raise ValueError(f"Non-numeric coordinate {pt} in '{file_path}'.")
            # Convert relative [0-1] or absolute pixel coords (>1)
            x = int(x_rel * w) if 0.0 <= x_rel <= 1.0 else int(x_rel)
            y = int(y_rel * h) if 0.0 <= y_rel <= 1.0 else int(y_rel)
            abs_pts.append([x, y])
        pts = np.array(abs_pts, dtype=np.int32).reshape(-1, 1, 2)
        mask = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(mask, [pts], 255)
        self.mask = mask

    def apply(self, frame):
        return cv2.bitwise_and(frame, frame, mask=self.mask)

class VideoWorker(threading.Thread):
    def __init__(self, source, masks, model, output_queue, cam_idx):
        super().__init__(daemon=True)
        self.cap = cv2.VideoCapture(source)
        ret, frame = self.cap.read()
        if not ret:
            raise RuntimeError(f"Cannot open video: {source}")
        self.masks = masks
        self.model = model
        self.queue = output_queue
        self.cam_idx = cam_idx

    def run(self):
        while True:
            ret, frame = self.cap.read()
            if not ret:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            results_list = []
            counts = []
            for mask in self.masks:
                roi = mask.apply(frame)
                res = self.model(roi)[0]
                boxes, cnt = [], 0
                for box in res.boxes:
                    cls_id = int(box.cls[0])
                    if cls_id in DETECT_CLASSES:
                        xyxy = box.xyxy[0].cpu().numpy().astype(int)
                        boxes.append((xyxy, cls_id))
                        cnt += 1
                results_list.append((roi, boxes))
                counts.append(cnt)
            self.queue.put((results_list, counts, self.cam_idx))

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Traffic Intersection NeuroDetector Demo")
        self.model = YOLO('yolo11s.pt')

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)

        # Statistics panel
        self.stats_list = QListWidget()
        layout.addWidget(self.stats_list, 1)

        # Video grid
        grid = QGridLayout()
        layout.addLayout(grid, 4)
        self.labels = []
        for i in range(4):
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.labels.append(lbl)
            grid.addWidget(lbl, i // 2, i % 2)

        # Prepare masks for each zone file
        cap = cv2.VideoCapture(VIDEO_PATHS[0])
        ret, frame = cap.read()
        cap.release()
        if not ret:
            raise RuntimeError("Failed to read initial frame from video.")
        frame_shape = frame.shape
        self.mask_loaders = [MaskLoader(f, frame_shape) for f in ZONE_FILES]

        # Start video workers
        self.queue = Queue()
        for idx, path in enumerate(VIDEO_PATHS):
            masks = self.mask_loaders[2*idx:2*idx+2]
            t = VideoWorker(path, masks, self.model, self.queue, idx)
            t.start()

        # Stats history
        self.history = {i: [] for i in range(4)}
        self.last_time = time.time()

        # Update timer
        timer = QTimer(self)
        timer.timeout.connect(self.update_frame)
        timer.start(30)

    def update_frame(self):
        try:
            results_list, counts, cam_idx = self.queue.get_nowait()
        except:
            return
        for i, (roi, boxes) in enumerate(results_list):
            disp = roi.copy()
            for (xyxy, cls_id) in boxes:
                x1, y1, x2, y2 = xyxy
                cv2.rectangle(disp, (x1, y1), (x2, y2), (0,255,0), 2)
                cv2.putText(disp, str(cls_id), (x1, y1-5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 1)
            # Convert to QImage and draw region name
            h, w = disp.shape[:2]
            img = QImage(disp.data, w, h, disp.strides[0], QImage.Format.Format_BGR888)
            pix = QPixmap.fromImage(img).scaled(
                self.labels[2*cam_idx+i].size(), Qt.AspectRatioMode.KeepAspectRatio)
            painter = QPainter(pix)
            painter.setPen(QColor('white'))
            painter.setFont(QFont('Arial', 14))
            painter.drawText(10, 30, REGION_NAMES[2*cam_idx+i])
            painter.end()
            self.labels[2*cam_idx+i].setPixmap(pix)

            # Update history
            idx = 2*cam_idx + i
            self.history[idx].append(counts[i])

        # Every 5 seconds update stats panel
        now = time.time()
        if now - self.last_time >= 5:
            self.stats_list.clear()
            for idx, hist in self.history.items():
                cur = hist[-1] if hist else 0
                avg = sum(hist) / len(hist) if hist else 0
                self.stats_list.addItem(
                    f"{REGION_NAMES[idx]} — Current: {cur}, Avg(5s): {avg:.1f}")
                self.history[idx].clear()
            self.last_time = now

if __name__ == '__main__':
    app = QApplication(sys.argv)
    win = MainWindow()
    win.showMaximized()
    sys.exit(app.exec())
