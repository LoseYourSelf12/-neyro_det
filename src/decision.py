import logging
from config import Config

class DecisionEngine:
    """
    Решение, нужно ли менять программу на основе загруженности.
    """
    def __init__(self, config: Config):
        self.threshold = config.get('analysis', 'congestion_threshold')
        self.downgrade_cycles = config.get('analysis', 'downgrade_cycles')
        self._log = logging.getLogger(self.__class__.__name__)
        self._no_congest_cycles = 0

    def decide(self, current_prog, avg_12, avg_34):
        new_prog = current_prog
        congest_12 = avg_12 > self.threshold
        congest_34 = avg_34 > self.threshold
        self._log.debug(f"Avg12={avg_12}, Avg34={avg_34}, thr={self.threshold}")

        # повышение
        if congest_12 and current_prog != 1:
            new_prog = 1
        elif congest_34 and current_prog != 2:
            new_prog = 2
        # понижение
        elif not congest_12 and not congest_34:
            self._no_congest_cycles += 1
            if self._no_congest_cycles >= self.downgrade_cycles and current_prog != 0:
                new_prog = 0
                self._no_congest_cycles = 0
        else:
            self._no_congest_cycles = 0

        if new_prog != current_prog:
            self._log.info(f"Decision: switch from {current_prog} to {new_prog}")
        return new_prog