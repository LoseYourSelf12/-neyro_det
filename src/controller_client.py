import requests
import logging
from config import Config

class ControllerClient:
    """
    Обёртка над HTTP-API контроллера светофора.
    Предполагаем два эндпоинта:
      GET  {base_url}/program      → текущая программа { "program": <int> }
      POST {base_url}/program      → смена программы с JSON { "program": <int> }
    """
    def __init__(self, config: Config):
        self._base = config.get('controller', 'api_base_url')
        self._timeout = config.get('controller', 'http_timeout', default=2)
        self._log = logging.getLogger(self.__class__.__name__)

    def get_current_program(self) -> int:
        """Вернуть ID текущей программы (0–6)."""
        url = f"{self._base}/program"
        try:
            r = requests.get(url, timeout=self._timeout)
            r.raise_for_status()
            data = r.json()
            program = data.get('program')
            self._log.debug(f"Current program from controller: {program}")
            return int(program)
        except Exception as e:
            self._log.error(f"Failed to get current program: {e}")
            raise

    def set_program(self, program_id: int) -> bool:
        """Поменять программу на program_id. Возвращает True при успехе."""
        url = f"{self._base}/program"
        payload = {'program': program_id}
        try:
            r = requests.post(url, json=payload, timeout=self._timeout)
            r.raise_for_status()
            self._log.info(f"Program changed to {program_id}")
            return True
        except Exception as e:
            self._log.error(f"Failed to set program to {program_id}: {e}")
            return False
        
    def get_phase_status(self) -> dict:
        """
        Запрос к /api/phase_status, возвращает dict:
          { "program": int, "phase": int, "time_left": float }
        """
        url = f"{self._base}/phase_status"
        r = requests.get(url, timeout=self._timeout)
        r.raise_for_status()
        return r.json()