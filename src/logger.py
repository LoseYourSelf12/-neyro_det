# src/logger.py
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Optional
from .config import Config

_NOISY_LOGGERS = ("ultralytics", "yolov5", "torch", "urllib3", "PIL")

class DuplicateFilter(logging.Filter):
    """Фильтр, режущий подряд идущие одинаковые сообщения в коротком окне."""
    def __init__(self, window_sec=0.3):
        super(DuplicateFilter, self).__init__()
        self.window = float(window_sec)
        self._last_msg = None
        self._last_ts = 0.0
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        now = time.time()
        with self._lock:
            if self._last_msg == msg and (now - self._last_ts) <= self.window:
                return False
            self._last_msg = msg
            self._last_ts = now
            return True

class _StdoutToLogger(object):
    """Прокси для перехвата print() -> logging."""
    def __init__(self, logger: logging.Logger, level: int = logging.INFO):
        self.logger = logger
        self.level = level
        self._buffer = ""  # копим до '\n'

    def write(self, s):
        if not isinstance(s, str):
            try:
                s = s.decode("utf-8", "ignore")
            except Exception:
                s = str(s)
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                # чтобы не зациклить — пишем в отдельный логгер 'stdout'
                self.logger.log(self.level, line)

    def flush(self):
        if self._buffer:
            line = self._buffer.strip()
            self._buffer = ""
            if line:
                self.logger.log(self.level, line)

def _remove_all_handlers(logger: logging.Logger):
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

def setup_logging(config: Config) -> None:
    level_name = config.get('logging', 'level', default='INFO')
    level = getattr(logging, level_name.upper(), logging.INFO)
    log_file = config.get('logging', 'file', default='logs/neyro_det.log')
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    max_bytes = config.get('logging', 'max_bytes', default=10_485_760)
    backup_count = config.get('logging', 'backup_count', default=5)

    root = logging.getLogger()
    root.setLevel(level)

    # 1) Полный reset хендлеров root
    _remove_all_handlers(root)

    # 2) “Обнуляем” шумные логгеры, чтобы не ставили свои хендлеры
    for name in _NOISY_LOGGERS:
        lg = logging.getLogger(name)
        _remove_all_handlers(lg)
        lg.propagate = True

    # 3) Фильтр дублей
    dup_filter = DuplicateFilter(window_sec=0.25)

    # 4) Консольный (stderr) хендлер
    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    console.addFilter(dup_filter)
    root.addHandler(console)

    # 5) Файловый (UTF-8) хендлер
    file_handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    file_handler.addFilter(dup_filter)
    root.addHandler(file_handler)

    # 6) Перехватываем stdout -> отдельный логгер, чтобы не было "голых" дублей
    #    stderr оставляем logging’у.
    stdout_logger = logging.getLogger("stdout")
    stdout_logger.propagate = True   # пусть идёт в root
    sys.stdout = _StdoutToLogger(stdout_logger, level=logging.INFO)

    root.debug("Logging initialized")
