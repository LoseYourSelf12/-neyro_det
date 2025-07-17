import logging
from logging.handlers import RotatingFileHandler
from config import Config


def setup_logging(config: Config) -> None:
    level_name = config.get('logging', 'level', default='INFO')
    level = getattr(logging, level_name.upper(), logging.INFO)
    log_file = config.get('logging', 'file', default='neyro_det.log')
    max_bytes = config.get('logging', 'max_bytes', default=10_485_760)
    backup_count = config.get('logging', 'backup_count', default=5)

    logger = logging.getLogger()
    logger.setLevel(level)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    console.setFormatter(console_fmt)
    logger.addHandler(console)

    # File handler
    file_handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
    file_handler.setLevel(level)
    file_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    logger.debug("Logging initialized")