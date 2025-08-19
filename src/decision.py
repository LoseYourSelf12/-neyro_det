import logging
from typing import Tuple
from .config import Config

class DecisionEngine:
    """State machine for selecting traffic light programs (4-11)."""

    def __init__(self, config: Config):
        analysis_cfg = config.get('analysis') or {}
        self.phase1_threshold = analysis_cfg.get('phase1_threshold', 0)
        self.phase2_threshold = analysis_cfg.get('phase2_threshold', 0)
        self.downgrade_cycles = analysis_cfg.get('downgrade_cycles', 1)
        self._log = logging.getLogger(self.__class__.__name__)

        # counters for consecutive cycles without congestion on each phase
        self._p1_ok_cycles = 0
        self._p2_ok_cycles = 0

        # last congestion flags captured at the end of phases
        self._p1_congest = False
        self._p2_congest = False

    @staticmethod
    def _decode_program(program: int) -> Tuple[int, bool]:
        """Return (p1_step, p2_extended) for program id."""
        if 4 <= program <= 7:
            return program - 4, False
        if 8 <= program <= 11:
            return program - 8, True
        return 0, False

    @staticmethod
    def _encode_program(p1_step: int, p2_extended: bool) -> int:
        base = 8 if p2_extended else 4
        return base + max(0, min(3, p1_step))

    def update_and_decide(self, phase: int, current_prog: int,
                           avg_main: float, avg_side: float) -> int:
        """Update congestion info for a phase and decide new program.

        The decision is made only after the second phase (phase==1), when we
        have information about queues on both approaches.
        """
        if phase == 0:
            # end of phase 1 (main road)
            self._p1_congest = avg_main > self.phase1_threshold
            self._log.debug(
                f"Phase1 avg={avg_main:.1f}, thr={self.phase1_threshold}, cong={self._p1_congest}"
            )
            return current_prog

        # phase 1 info already stored; now handle phase 2
        self._p2_congest = avg_side > self.phase2_threshold
        self._log.debug(
            f"Phase2 avg={avg_side:.1f}, thr={self.phase2_threshold}, cong={self._p2_congest}"
        )

        p1_step, p2_ext = self._decode_program(current_prog)

        # update counters
        if self._p1_congest:
            self._p1_ok_cycles = 0
        else:
            self._p1_ok_cycles += 1
        if self._p2_congest:
            self._p2_ok_cycles = 0
        else:
            self._p2_ok_cycles += 1

        if self._p1_congest and self._p2_congest:
            p2_ext = True
            if p1_step < 3:
                p1_step += 1
        elif self._p1_congest and not self._p2_congest:
            if p2_ext and self._p2_ok_cycles >= self.downgrade_cycles:
                p2_ext = False
            elif not p2_ext and p1_step < 3:
                p1_step += 1
        elif self._p2_congest and not self._p1_congest:
            p2_ext = True
            if self._p1_ok_cycles >= self.downgrade_cycles:
                p1_step = 0
        else:  # neither congested
            if self._p1_ok_cycles >= self.downgrade_cycles and p1_step > 0:
                p1_step -= 1
                self._p1_ok_cycles = 0
            if self._p2_ok_cycles >= self.downgrade_cycles and p2_ext:
                p2_ext = False
                self._p2_ok_cycles = 0

        new_prog = self._encode_program(p1_step, p2_ext)
        if new_prog != current_prog:
            self._log.info(f"Decision: switch from {current_prog} to {new_prog}")

        return new_prog
