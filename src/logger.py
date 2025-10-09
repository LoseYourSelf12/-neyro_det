import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from .config import Config

# какие логгеры часто добавляют СВОИ хендлеры
_NOISY_LOGGERS = (
    "ultralytics", "yolov5", "torch", "urllib3", "PIL",
)

def _remove_all_handlers(logger):
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

def setup_logging(config: Config) -> None:
    level_name = config.get('logging', 'level', default='INFO')
    level = getattr(logging, level_name.upper(), logging.INFO)
    log_file = config.get('logging', 'file', default='neyro_det.log')
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    max_bytes = config.get('logging', 'max_bytes', default=10_485_760)
    backup_count = config.get('logging', 'backup_count', default=5)

    root = logging.getLogger()
    root.setLevel(level)

    # 1) Полностью очищаем существующие хендлеры, чтобы не было дублей
    _remove_all_handlers(root)

    # 2) Удаляем локальные хендлеры у «шумных» логгеров и разрешаем им
    #    пропускать сообщения вверх (в root), чтобы формат был единый
    for name in _NOISY_LOGGERS:
        lg = logging.getLogger(name)
        _remove_all_handlers(lg)
        lg.propagate = True
        # не меняем их уровень, пусть наследуют от root

    # 3) Консольный хендлер
    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    root.addHandler(console)

    # 4) Файловый хендлер (всегда UTF-8, чтобы не ловить UnicodeEncodeError в файлах)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    root.addHandler(file_handler)

    root.debug("Logging initialized")
