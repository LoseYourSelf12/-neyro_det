import sys
import cv2
import yaml
import random
import numpy as np

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QSlider, QListWidget,
    QLineEdit, QMessageBox, QListWidgetItem, QComboBox
)
from PyQt6.QtGui import QPixmap, QImage, QMouseEvent
from PyQt6.QtCore import Qt, QTimer, QSize

def random_color():
    """Генерирует случайный цвет (B, G, R) для OpenCV."""
    return tuple(random.randint(0, 255) for _ in range(3))

class FlowList(list):
    pass

def flow_representer(dumper, data):
    return dumper.represent_sequence(u'tag:yaml.org,2002:seq', data, flow_style=True)

yaml.add_representer(FlowList, flow_representer)

class VideoLabel(QLabel):
    """
    Класс для отображения видео и обработки событий мыши/клавиатуры.
    """
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        # Разрешаем фокус, чтобы обрабатывать нажатия клавиш
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            # Добавляем точку
            self.editor.add_point(event.pos())
        elif event.button() == Qt.MouseButton.RightButton:
            # Замыкаем контур
            self.editor.close_polygon()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        # Backspace удаляет последнюю точку, если контур не замкнут
        if event.key() == Qt.Key.Key_Backspace:
            self.editor.undo_point()
        super().keyPressEvent(event)

class ZoneEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Zone Editor")
        self.resize(1000, 600)

        # Переменные для работы с видео
        self.cap = None
        self.timer = QTimer()
        self.playing = False
        self.current_frame_index = 0
        self.total_frames = 0
        self.video_width = 0
        self.video_height = 0

        # Размеры для отображения (по умолчанию)
        self.display_width = 640
        self.display_height = 360

        # Переменные для хранения зон и групп
        self.zones = []  # [{id, group_id, points=[(x,y), ...]}]
        self.groups = {}  # {group_id: (B, G, R)}
        self.current_zone_points = []
        self.selected_group_id = None

        # Инициализация UI
        self.init_ui()

        # Сигналы/слоты
        self.timer.timeout.connect(self.next_frame)

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # Левая часть - видео, слайдер, кнопки управления
        left_layout = QVBoxLayout()

        # Комбо-бокс выбора разрешения
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem("640 x 360", (640, 360))
        self.resolution_combo.addItem("960 x 540", (960, 540))
        self.resolution_combo.addItem("1280 x 720", (1280, 720))
        self.resolution_combo.addItem("1920 x 1080", (1920, 1080))
        self.resolution_combo.currentIndexChanged.connect(self.on_resolution_selected)
        left_layout.addWidget(self.resolution_combo)

        # Метка для показа видео
        self.video_label = VideoLabel(self)
        self.video_label.setFixedSize(self.display_width, self.display_height)
        self.video_label.setStyleSheet("background-color: black;")
        left_layout.addWidget(self.video_label)

        # Слайдер для перемотки
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self.slider_changed)
        left_layout.addWidget(self.slider)

        # Кнопки управления (Play/Pause, Load Video)
        controls_layout = QHBoxLayout()
        self.play_button = QPushButton("Play/Pause")
        self.play_button.clicked.connect(self.toggle_play)
        controls_layout.addWidget(self.play_button)

        self.load_button = QPushButton("Load Video")
        self.load_button.clicked.connect(self.load_video)
        controls_layout.addWidget(self.load_button)

        left_layout.addLayout(controls_layout)

        main_layout.addLayout(left_layout)

        # Правая часть - список групп, кнопки, сохранение
        right_layout = QVBoxLayout()

        # Список групп
        self.group_list = QListWidget()
        self.group_list.currentItemChanged.connect(self.on_group_selected)
        right_layout.addWidget(self.group_list)

        # Поле для ввода имени группы
        self.group_name_edit = QLineEdit()
        self.group_name_edit.setPlaceholderText("Имя новой группы (необязательно)")
        right_layout.addWidget(self.group_name_edit)

        # Кнопка добавления группы
        self.add_group_button = QPushButton("+ Добавить группу")
        self.add_group_button.clicked.connect(self.add_group)
        right_layout.addWidget(self.add_group_button)

        # Кнопка сохранения
        self.save_button = QPushButton("Save Zones")
        self.save_button.clicked.connect(self.save_zones)
        right_layout.addWidget(self.save_button)

        # Заполняем пустое пространство
        right_layout.addStretch()

        main_layout.addLayout(right_layout)

    def on_resolution_selected(self):
        """
        Метод, который вызывается при выборе пункта в ComboBox.
        Меняет масштаб отображения.
        """
        w, h = self.resolution_combo.currentData()
        self.display_width, self.display_height = w, h
        self.video_label.setFixedSize(self.display_width, self.display_height)
        # Перерисовываем текущий кадр, если есть
        if self.cap and self.current_frame_index < self.total_frames:
            self.show_frame(self.current_frame_index)

    def load_video(self):
        """
        Диалог выбора видео.
        """
        file_dialog = QFileDialog()
        video_path, _ = file_dialog.getOpenFileName(
            self, "Open Video File", "", "Videos (*.mp4 *.avi *.mov *.mkv)"
        )
        if video_path:
            self.cap = cv2.VideoCapture(video_path)
            if not self.cap.isOpened():
                QMessageBox.warning(self, "Error", "Failed to open video")
                return

            self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self.video_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.video_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

            # Настраиваем слайдер
            self.slider.setRange(0, self.total_frames - 1)
            self.slider.setValue(0)

            # Сброс счётчика
            self.current_frame_index = 0
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

            # Показываем первый кадр
            self.show_frame(self.current_frame_index)

    def toggle_play(self):
        if self.cap is None:
            return
        self.playing = not self.playing
        if self.playing:
            self.timer.start(30)  # ~33 кадров/сек (30 мс)
        else:
            self.timer.stop()

    def next_frame(self):
        if self.cap is None:
            return
        # Следующий кадр
        self.current_frame_index += 1
        if self.current_frame_index >= self.total_frames:
            self.current_frame_index = self.total_frames - 1
            self.playing = False
            self.timer.stop()
        self.show_frame(self.current_frame_index)
        self.slider.setValue(self.current_frame_index)

    def slider_changed(self):
        if self.cap is None:
            return
        value = self.slider.value()
        self.current_frame_index = value
        self.show_frame(self.current_frame_index)

    def show_frame(self, frame_index: int):
        """
        Считываем кадр с индексом frame_index из self.cap,
        рисуем зоны и масштабируем под (display_width, display_height).
        """
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ret, frame = self.cap.read()
        if not ret:
            return

        frame_drawn = self.draw_zones(frame)
        frame_rgb = cv2.cvtColor(frame_drawn, cv2.COLOR_BGR2RGB)
        frame_resized = cv2.resize(
            frame_rgb, 
            (self.display_width, self.display_height),
            interpolation=cv2.INTER_AREA
        )
        h, w, ch = frame_resized.shape
        bytes_per_line = ch * w
        q_img = QImage(
            frame_resized.data, w, h,
            bytes_per_line,
            QImage.Format.Format_RGB888
        )
        pix = QPixmap.fromImage(q_img)
        self.video_label.setPixmap(pix)

    def draw_zones(self, frame):
        """
        Рисует уже созданные зоны и текущий редактируемый контур на кадре.
        Координаты точек — в масштабе исходного видео.
        """
        overlay = frame.copy()
        # Существующие зоны
        for zone in self.zones:
            color = self.groups.get(zone["group_id"], (0, 255, 0))
            points_np = np.array(zone["points"], dtype=np.int32).reshape((-1, 1, 2))
            cv2.polylines(overlay, [points_np], isClosed=True, color=color, thickness=2)

        # Текущие незамкнутые точки
        if self.current_zone_points:
            # Линии между точками
            for i in range(len(self.current_zone_points) - 1):
                cv2.line(
                    overlay,
                    self.current_zone_points[i],
                    self.current_zone_points[i+1],
                    (0, 0, 255), 2
                )
            # Сами точки
            for pt in self.current_zone_points:
                cv2.circle(overlay, pt, 3, (0, 0, 255), -1)

        return overlay

    def add_point(self, pos):
        """
        Добавляем точку в текущий полигон с учётом масштабирования.
        pos — QPoint в координатах VideoLabel (scaled).
        """
        if self.cap is None:
            return
        # Переводим экранные координаты в «оригинальные»
        scale_x = self.video_width / self.display_width
        scale_y = self.video_height / self.display_height

        real_x = int(pos.x() * scale_x)
        real_y = int(pos.y() * scale_y)

        self.current_zone_points.append((real_x, real_y))
        self.show_frame(self.current_frame_index)

    def close_polygon(self):
        """
        Замыкаем текущий контур. Если меньше 3 точек, не замыкаем.
        """
        if len(self.current_zone_points) < 3:
            return
        zone_id = len(self.zones) + 1
        self.zones.append({
            "id": zone_id,
            "group_id": self.selected_group_id,
            "points": self.current_zone_points.copy()
        })
        self.current_zone_points.clear()
        self.show_frame(self.current_frame_index)

    def undo_point(self):
        """
        Удаление последней добавленной точки, если контур не замкнут.
        """
        if self.current_zone_points:
            self.current_zone_points.pop()
            self.show_frame(self.current_frame_index)

    def add_group(self):
        """
        Создание новой группы с уникальным ID и случайным цветом.
        """
        group_id = len(self.groups) + 1
        color = random_color()
        self.groups[group_id] = color

        # Используем введённое имя или «Group {id}» по умолчанию
        name = self.group_name_edit.text().strip()
        if not name:
            name = f"Group {group_id}"
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, group_id)
        self.group_list.addItem(item)
        # Делаем эту группу выбранной
        self.group_list.setCurrentItem(item)
        self.group_name_edit.clear()

    def on_group_selected(self, current, previous):
        """
        Слот, который вызывается при смене выбранного элемента в списке групп.
        """
        if current:
            self.selected_group_id = current.data(Qt.ItemDataRole.UserRole)
        else:
            self.selected_group_id = None

    def save_zones(self):
        """
        Выбор пути сохранения YAML-файла и запись данных.
        """
        if not self.zones:
            QMessageBox.information(self, "Info", "Нет зон для сохранения.")
            return

        file_dialog = QFileDialog()
        save_path, _ = file_dialog.getSaveFileName(
            self, "Save Zones", "zones.yaml", "YAML Files (*.yaml *.yml)"
        )
        if not save_path:
            return

        zones_serialized = []
        for z in self.zones:
            # Преобразуем координаты к float
            points_flow = FlowList([FlowList(float(coord) for coord in pt) for pt in z["points"]])
            zone_dict = {
                "id": z["id"],
                "group_id": z["group_id"],
                "points": points_flow
            }
            zones_serialized.append(zone_dict)

        data = {"zones": zones_serialized}

        with open(save_path, "w", encoding="utf-8") as f:
            yaml.dump(
                data, f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
                width=9999
            )

        QMessageBox.information(self, "Saved", f"Зоны успешно сохранены в:\n{save_path}")


def main():
    app = QApplication(sys.argv)
    editor = ZoneEditor()
    editor.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
