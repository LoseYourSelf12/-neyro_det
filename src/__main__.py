# src/__main__.py
import time
import logging
from config import Config
from logger import setup_logging
from controller_client import ControllerClient
from video_capture import VideoCapture
from detector import Detector
from analyzer import average_counts
from decision import DecisionEngine

def do_detection_cycle(vc, detector, decision, ctrl, logger, phase):
    """
    Захват N кадров, подсчёт машин, решение и смена программы.
    phase: текущая фаза светофора (0 или 1).
    """
    shots = cfg.get('analysis', 'shots_per_phase')
    counts_main, counts_side = [], []
    for _ in range(shots):
        f1 = vc.read('1'); f2 = vc.read('2')
        counts_main.append(len(detector.predict(f1)) + len(detector.predict(f2)))
        f3 = vc.read('3')
        counts_side.append(len(detector.predict(f3)))

    avg_main = average_counts(counts_main)
    avg_side = average_counts(counts_side)
    prog = ctrl.get_current_program()
    new_prog = decision.update_and_decide(phase, prog, avg_main, avg_side)

    if new_prog != prog:
        ctrl.set_program(new_prog)

    logger.info(
        f"Cycle complete: prog={prog}, phase={phase}, "
        f"avg_main={avg_main:.1f}, avg_side={avg_side:.1f}, new={new_prog}"
    )

if __name__ == '__main__':
    # Загрузка конфига и логгера
    cfg = Config()
    setup_logging(cfg)
    log = logging.getLogger()

    # Инициализация модулей
    ctrl = ControllerClient(cfg)
    vc = VideoCapture(cfg)
    det = Detector(cfg)
    dec = DecisionEngine(cfg)

    lead = cfg.get('controller', 'traffic_phase_lead_sec', default=2)
    log.info("Starting neyro_det service...")

    try:
        while True:
            status = ctrl.get_phase_status()
            prog = status['program']
            phase = status['phase']
            time_left = status['time_left']
            log.info(f"Checking object phase...\nphase={phase}, time_left={time_left:.1f}s")
            log.debug(f"Prog={prog}, phase={phase}, time_left={time_left:.1f}s")

            # Когда до конца зелёного остаётся <= lead и после этой фазы включается красный
            if phase in (0, 1) and time_left <= lead:
                do_detection_cycle(vc, det, dec, ctrl, log, phase)
                # чтобы не повторяться в одной фазе
                time.sleep(lead + 0.1)
            else:
                time.sleep(0.2)

    except KeyboardInterrupt:
        log.info("Shutting down neyro_det service")
