import json
import os
from typing import Any, Dict

class Config:
    """
    Загрузчик конфигурации из JSON-файла.
    """
    def __init__(self, path: str = "config/default.json"):
        self._path = path
        self._data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not os.path.isfile(self._path):
            raise FileNotFoundError(f"Config file not found: {self._path}")
        with open(self._path, 'r', encoding='utf-8') as f:
            self._data = json.load(f)

    def get(self, *keys, default: Any = None) -> Any:
        data = self._data
        for key in keys:
            if isinstance(data, dict) and key in data:
                data = data[key]
            else:
                return default
        return data

    def reload(self) -> None:
        """Перезагрузить конфиг вручную."""
        self.load()