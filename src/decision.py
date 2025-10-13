import logging
from dataclasses import dataclass
from typing import Dict, Tuple, Optional, List
import time

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


# --- switch guard: анти-дребезг команд на контроллер ---
class SwitchGuard(object):
    """
    Блокирует повторные смены плана, пока предыдущая не подтвердится мониторингом,
    и держит минимальную паузу между успешными сменами.
    """
    def __init__(self,
                 min_interval_sec=30,          # минимальная пауза после успешной смены
                 confirm_timeout_sec=120,      # сколько ждём подтверждение от ДК
                 confirm_need_frames=2):       # сколькими подряд кадрами подтвердить
        self.min_interval_sec = int(min_interval_sec)
        self.confirm_timeout_sec = int(confirm_timeout_sec)
        self.confirm_need_frames = int(confirm_need_frames)

        self.pending_plan = None              # int | None
        self.pending_since = 0.0
        self._confirm_hits = 0
        self.last_success_at = 0.0
        self._last_phase = None
        self._last_takt = None

    def allow_new_decision(self, now_ts):
        """Можно ли инициировать новое переключение прямо сейчас?"""
        if self.pending_plan is not None:
            return False
        if self.last_success_at and (now_ts - self.last_success_at) < self.min_interval_sec:
            return False
        return True

    def start_switch(self, target_plan, now_ts):
        """Зафиксировать начало переключения (после успешной отправки gNN)."""
        self.pending_plan = int(target_plan)
        self.pending_since = float(now_ts)
        self._confirm_hits = 0
        return True

    def observe_monitor(self, st):
        """
        Кормим каждый кадр мониторинга.
        st: dict со статусом (program, phase, takt, cycle_second, time_left...)
        Возвращает одно из: 'pending' | 'confirmed' | 'failed' | 'idle'
        """
        if self.pending_plan is None:
            # просто запоминаем последние фазу/такт (для детекции рестарта цикла)
            self._last_phase = st.get("phase")
            self._last_takt = st.get("takt")
            return "idle"

        now_ts = time.time()

        # таймаут ожидания
        if (now_ts - self.pending_since) > self.confirm_timeout_sec:
            self.pending_plan = None
            self._confirm_hits = 0
            return "failed"

        cur_prog = int(st.get("program") or 0)
        cur_phase = st.get("phase")
        cur_takt = st.get("takt")

        # подтверждаем либо самим планом, либо признаком рестарта цикла
        restart_cycle = (self._last_phase, self._last_takt) != (cur_phase, cur_takt) and (cur_phase == 1 and cur_takt == 1)
        self._last_phase, self._last_takt = cur_phase, cur_takt

        if cur_prog == self.pending_plan or restart_cycle:
            self._confirm_hits += 1
            if self._confirm_hits >= self.confirm_need_frames:
                # подтверждено
                self.pending_plan = None
                self._confirm_hits = 0
                self.last_success_at = now_ts
                return "confirmed"
            return "pending"

        # ещё не подтвердилось
        self._confirm_hits = 0
        return "pending"


@dataclass
class DecisionParams:
    thr_main: int = 5              # порог очереди для main
    thr_side: int = 3              # порог очереди для side
    sample_window_sec: float = 3.0 # окно “под конец зелёного” (для съёмки очереди)
    downgrade_cycles: int = 2      # сколько подряд “спокойных” циклов нужно, чтобы понижать
    min_program: int = 4           # нижняя граница автоподбора (1..3 — техн., не трогаем)
    max_program: int = 16


class DecisionEngine:
    """
    Двухосевой решатель на сетке программ.
    По каждой оси (main/side) ведём свой счётчик спокойных циклов; повышаем при перегрузе,
    понижаем после серии спокойных. Отправку команд защищает SwitchGuard.
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

        c = (cfg.get("controller") or {})
        min_interval = int(c.get("min_switch_interval_sec", 30))
        confirm_timeout = int(c.get("switch_confirm_timeout_sec", 120))
        confirm_frames = int(c.get("switch_confirm_frames", 2))
        self._guard = SwitchGuard(min_interval, confirm_timeout, confirm_frames)

    # --- API guard’а для main ---
    def guard_allow(self) -> bool:
        return self._guard.allow_new_decision(time.time())

    def guard_start(self, target_plan: int) -> None:
        self._guard.start_switch(target_plan, time.time())

    def guard_observe(self, st: Dict[str, int]) -> str:
        return self._guard.observe_monitor(st)

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

    def decide(self, current_program: int, q_main: int, q_side: int) -> Optional[int]:
        """
        Возвращает новую программу или None (если менять не требуется/нельзя).
        q_main — очередь, измеренная под конец фазы 1 (main становится красным)
        q_side — очередь, измеренная под конец фазы 2 (side становится красным)
        """
        # тех. режимы 1..3 не трогаем
        if current_program < self.params.min_program:
            return None

        # если переключение в процессе или выдерживаем паузу — только логируем
        if not self.guard_allow():
            self._log.debug("Switch pending or in cooldown; skip decision")
            return None

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
            if new_i == 4 and j < 3:
                # (4,0..2) не существует — остаёмся на прежней допустимой клетке
                new_i = i
        elif congest_side and not congest_main:
            # усиливаем side
            if j < 3:
                new_j = min(3, j + 1)
                # j==3 допустим только при i==4
                if new_j == 3 and i < 4:
                    new_j = 2
            else:
                new_i, new_j = 4, 3
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
        return None
