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

def do_detection_cycle(vc, detector, decision, ctrl, logger):
    """
    Захват N кадров, подсчёт машин, решение и смена программы.
    """
    shots = cfg.get('analysis', 'shots_per_phase')
    counts_12, counts_34 = [], []
    for _ in range(shots):
        f1 = vc.read('1'); f2 = vc.read('2')
        counts_12.append(len(detector.predict(f1)) + len(detector.predict(f2)))
        f3 = vc.read('3'); f4 = vc.read('4')
        counts_34.append(len(detector.predict(f3)) + len(detector.predict(f4)))

    avg_12 = average_counts(counts_12)
    avg_34 = average_counts(counts_34)
    prog = ctrl.get_current_program()
    new_prog = decision.decide(prog, avg_12, avg_34)

    if new_prog != prog:
        ctrl.set_program(new_prog)

    logger.info(f"Cycle complete: prog={prog}, avg12={avg_12:.1f}, avg34={avg_34:.1f}, new={new_prog}")

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
                print('1')
                do_detection_cycle(vc, det, dec, ctrl, log)
                # чтобы не повторяться в одной фазе
                time.sleep(lead + 0.1)
            else:
                time.sleep(0.2)

    except KeyboardInterrupt:
        log.info("Shutting down neyro_det service")
