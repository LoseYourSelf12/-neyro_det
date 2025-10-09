import logging
from dataclasses import dataclass
from typing import Dict, Tuple, Optional, List

from .config import Config

# Матрица программ: (i_main, j_side) -> program_id
# main_levels = [70, 75, 80, 90, 100]  (индекс i: 0..4)
# side_levels = [16, 26, 35, 40]       (индекс j: 0..3)
_PROGRAM_BY_IDX: Dict[Tuple[int, int], int] = {
    (0, 0): 4,  (1, 0): 5,  (2, 0): 6,  (3, 0): 7,
    (0, 1): 8,  (1, 1): 9,  (2, 1): 10, (3, 1): 11,
    (0, 2): 12, (1, 2): 13, (2, 2): 14, (3, 2): 15,
    (4, 3): 16,  # единственная комбинация для (100,40)
}

_IDX_BY_PROGRAM: Dict[int, Tuple[int, int]] = {p: ij for ij, p in _PROGRAM_BY_IDX.items()}
_MAIN_LEVELS: List[int] = [70, 75, 80, 90, 100]
_SIDE_LEVELS: List[int] = [16, 26, 35, 40]

@dataclass
class DecisionParams:
    thr_main: int = 5              # порог очереди для main
    thr_side: int = 3              # порог очереди для side
    sample_window_sec: float = 3.0 # окно “под конец зелёного”
    downgrade_cycles: int = 2      # сколько подряд “спокойных” циклов нужно, чтобы понижать
    min_program: int = 4           # нижняя граница автоподбора (1..3 — техн., не трогаем)
    max_program: int = 16

class DecisionEngine:
    """
    Двухосевой решатель на сетке программ.
    По каждой оси (main/side) ведём свой счётчик спокойных циклов; повышаем при перегрузе,
    понижаем после серии спокойных.
    """
    def __init__(self, cfg: Config):
        a = (cfg.get("analysis") or {})
        self.params = DecisionParams(
            thr_main = int(a.get("phase1_threshold", 5)),
            thr_side = int(a.get("phase2_threshold", 3)),
            sample_window_sec = float(a.get("sample_window_sec", 3.0)),
            downgrade_cycles = int(a.get("downgrade_cycles", 2)),
            min_program = 4,
            max_program = 16,
        )
        self._log = logging.getLogger(self.__class__.__name__)
        self._ok_main = 0
        self._ok_side = 0

    @staticmethod
    def _idx_from_prog(p: int) -> Optional[Tuple[int, int]]:
        return _IDX_BY_PROGRAM.get(p)

    @staticmethod
    def _prog_from_idx(i: int, j: int) -> int:
        # нормализация в доступную ячейку
        if (i, j) in _PROGRAM_BY_IDX:
            return _PROGRAM_BY_IDX[(i, j)]
        # ряд j==3 доступен только при i==4 => сводим к 16
        if j >= 3 or i >= 4:
            return 16
        # иначе ограничим в пределах матрицы 0..3 x 0..2
        i = max(0, min(3, i))
        j = max(0, min(2, j))
        return _PROGRAM_BY_IDX[(i, j)]

    def decide(self, current_program: int, q_main: int, q_side: int) -> int:
        """
        Возвращает новую программу (может совпасть с текущей).
        q_main — очередь, измеренная под конец фазы 1 (main становится красным)
        q_side — очередь, измеренная под конец фазы 2 (side становится красным)
        """
        # тех. режимы 1..3 не трогаем
        if current_program < self.params.min_program:
            return current_program

        idx = self._idx_from_prog(current_program)
        if not idx:
            # если пришёл неожиданный (но валидный) номер — приблизим к сетке
            if current_program >= 16:
                idx = (4, 3)
            else:
                idx = (0, 0)
        i, j = idx

        congest_main = q_main > self.params.thr_main
        congest_side = q_side > self.params.thr_side

        # счётчики спокойствия
        self._ok_main = 0 if congest_main else self._ok_main + 1
        self._ok_side = 0 if congest_side else self._ok_side + 1

        new_i, new_j = i, j

        if congest_main and congest_side:
            # растём к (100,40)=16 по диагонали
            if j < 3 and i < 4:
                new_i = min(4, i + 1)
                new_j = min(3, j + 1)
            else:
                new_i, new_j = 4, 3
        elif congest_main and not congest_side:
            # усиливаем main
            new_i = min(4, i + 1)
            # если упёрлись в 100, j оставляем; (4,0..2) не существует — свернём в 16 только если side тоже упирается
            if new_i == 4 and j < 3:
                # остаёмся на ближайшей доступной клетке — фактически вернём прежнюю
                new_i = i
        elif congest_side and not congest_main:
            # усиливаем side
            if j < 3:
                new_j = min(3, j + 1)
                # переход на j==3 допустим только при i==4 => если i<4 — останемся на j<=2
                if new_j == 3 and i < 4:
                    new_j = 2
            else:
                new_i, new_j = 4, 3  # на всякий случай (вверх до 16)
        else:
            # оба спокойны — понижаемся постепенно
            if self._ok_main >= self.params.downgrade_cycles and i > 0:
                new_i = i - 1
                self._ok_main = 0
            if self._ok_side >= self.params.downgrade_cycles and j > 0:
                new_j = j - 1
                self._ok_side = 0
            # с 16 спускаться: j 3->2, i 4->3 при накоплении ок-циклов
            if (i, j) == (4, 3):
                if self._ok_side >= self.params.downgrade_cycles:
                    new_i, new_j = 3, 2
                    self._ok_side = 0

        new_prog = self._prog_from_idx(new_i, new_j)
        if new_prog != current_program:
            self._log.info("Decision: %s -> %s  (q_main=%s thr=%s; q_side=%s thr=%s)",
                           current_program, new_prog, q_main, self.params.thr_main, q_side, self.params.thr_side)
        return max(self.params.min_program, min(self.params.max_program, new_prog))
