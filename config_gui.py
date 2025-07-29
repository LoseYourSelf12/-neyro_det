# -*- coding: utf-8 -*-
"""Simple PyQt6 GUI to edit config/default.json"""
import json
import os
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QCheckBox, QFileDialog, QLabel,
    QTableWidget, QTableWidgetItem, QMessageBox
)
from PyQt6.QtCore import Qt


class ConfigEditor(QWidget):
    def __init__(self, path: str):
        super().__init__()
        self.path = path
        with open(self.path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)

        self.safemode = QCheckBox("Safe mode")
        self.safemode.setChecked(True)
        self.safemode.stateChanged.connect(self.update_editable)

        self.form_layout = QFormLayout()
        self.edit_fields = {}

        self.camera_table = QTableWidget(0, 2)
        self.camera_table.setHorizontalHeaderLabels(["ID", "URL"])

        self.add_cam_btn = QPushButton("Add camera")
        self.del_cam_btn = QPushButton("Delete camera")
        self.add_cam_btn.clicked.connect(self.add_camera)
        self.del_cam_btn.clicked.connect(self.delete_camera)

        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self.save)

        layout = QVBoxLayout(self)
        layout.addWidget(self.safemode)
        layout.addWidget(QLabel("Cameras:"))
        layout.addWidget(self.camera_table)
        btns = QHBoxLayout()
        btns.addWidget(self.add_cam_btn)
        btns.addWidget(self.del_cam_btn)
        layout.addLayout(btns)
        layout.addWidget(QLabel("Other parameters:"))
        layout.addLayout(self.form_layout)
        layout.addWidget(self.save_btn)

        self.build_form()
        self.load_cameras()
        self.update_editable()
        self.setWindowTitle(f"Config editor - {os.path.basename(self.path)}")
        self.resize(600, 400)

    def build_form(self):
        def add_entries(obj, path):
            for key, value in obj.items():
                if key == 'cameras':
                    continue
                if isinstance(value, dict):
                    add_entries(value, path + [key])
                else:
                    label = '.'.join(path + [key])
                    line = QLineEdit(str(value))
                    self.form_layout.addRow(label, line)
                    self.edit_fields[tuple(path + [key])] = line
        add_entries(self.data, [])

    def load_cameras(self):
        cams = self.data.get('cameras', {})
        rows = len(cams)
        self.camera_table.setRowCount(rows)
        for row, (cid, url) in enumerate(sorted(cams.items(), key=lambda x: int(x[0]))):
            self.camera_table.setItem(row, 0, QTableWidgetItem(str(cid)))
            self.camera_table.setItem(row, 1, QTableWidgetItem(url))

    def add_camera(self):
        ids = []
        for row in range(self.camera_table.rowCount()):
            item = self.camera_table.item(row, 0)
            if item:
                try:
                    ids.append(int(item.text()))
                except ValueError:
                    pass
        new_id = max(ids + [0]) + 1
        row = self.camera_table.rowCount()
        self.camera_table.insertRow(row)
        self.camera_table.setItem(row, 0, QTableWidgetItem(str(new_id)))
        self.camera_table.setItem(row, 1, QTableWidgetItem("rtsp://"))

    def delete_camera(self):
        selected = {idx.row() for idx in self.camera_table.selectedIndexes()}
        for row in sorted(selected, reverse=True):
            self.camera_table.removeRow(row)

    def update_editable(self):
        editable = not self.safemode.isChecked()
        for line in self.edit_fields.values():
            line.setEnabled(editable)

    def save(self):
        # cameras
        cams = {}
        for row in range(self.camera_table.rowCount()):
            id_item = self.camera_table.item(row, 0)
            url_item = self.camera_table.item(row, 1)
            if id_item and url_item:
                cid = id_item.text().strip()
                url = url_item.text().strip()
                if cid:
                    cams[cid] = url
        self.data['cameras'] = cams

        # other fields
        for path, line in self.edit_fields.items():
            value_str = line.text()
            try:
                value = json.loads(value_str)
            except Exception:
                value = value_str
            target = self.data
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = value

        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=4, ensure_ascii=False)
        QMessageBox.information(self, "Saved", f"Config saved to {self.path}")


def select_config() -> str:
    default_path = os.path.join(os.path.dirname(__file__), 'config', 'default.json')
    if os.path.isfile(default_path):
        return default_path
    app = QApplication.instance() or QApplication([])
    file_path, _ = QFileDialog.getOpenFileName(None, "Select config", "", "JSON files (*.json)")
    if not file_path:
        raise SystemExit("No config selected")
    return file_path


def main():
    import sys
    app = QApplication(sys.argv)
    path = select_config()
    editor = ConfigEditor(path)
    editor.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
