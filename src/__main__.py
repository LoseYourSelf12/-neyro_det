import logging
import shutil
import time
import traceback
from pathlib import Path
from .config import Config
from .controller_client import ControllerClient
from .decision import DecisionEngine
from .detector import Detector
from .logger import setup_logging
from .service_utils import count_direction, setup_progchange_logger
from .snapshots import format_bytes, is_saving_allowed, space_ok, today_dir
from .video_capture import VideoCapture


# ---------- главный сервис ----------
def main():
    cfg = Config()
    setup_logging(cfg)
    log = logging.getLogger("service")
    proglog = setup_progchange_logger(cfg)  # отдельный файл смен программ

    # Контроллер
    client = ControllerClient(cfg)
    log.info("Controller connected and ready")

    # Решатель + guard
    engine = DecisionEngine(cfg)

    # Детектор
    detector = None
    try:
        detector = Detector(cfg)
        log.info("Detector model loaded")
    except Exception as e:
        log.warning("Detector unavailable, using random counts: %s", e)
        traceback.print_exc()

    # Видео
    vc = VideoCapture(cfg)

    # Снимки: включение/лог
    save_root = Path(cfg.get("snapshots", "save_dir", default="/media/MYUSB/neyro_det"))
    min_free_gb = float(cfg.get("snapshots", "min_free_gb", default=1.0))
    saving_globally_enabled = is_saving_allowed(save_root, min_free_gb, log)
    if saving_globally_enabled:
        free = shutil.disk_usage(save_root).free
        log.info("Snapshots ENABLED at: %s (free: %s, threshold: %.2f GB)",
                 save_root.resolve(), format_bytes(free), min_free_gb)
    else:
        log.error("Snapshots DISABLED: saving will be skipped (no fallback).")

    # Настройки анализа
    shots_per_probe = int(cfg.get("analysis", "shots_per_phase", default=1))
    poll_period = float(cfg.get("analysis", "poll_period_sec", default=0.5))
    # детект делаем ПОСЛЕ начала красного
    red_delay = float(cfg.get("analysis", "red_sample_delay_sec", default=5.0))

    # Окно отправки и карты камер
    lead_sec = int(cfg.get("controller", "traffic_phase_lead_sec", default=2))
    main_cams = cfg.get("analysis", "main_cameras", default=["1"]) or ["1"]
    side_cams = cfg.get("analysis", "side_cameras", default=["2", "3"]) or ["2", "3"]

    # Состояние цикла / фаз
    prev_phase = None
    cycle_id = 0

    # метки начала красного и «уже сэмплировали в этом цикле»
    main_red_t0 = None
    side_red_t0 = None
    main_sampled_cycle = -1
    side_sampled_cycle = -1

    last_q_main = 0
    last_q_side = 0

    # «Вооружённое» переключение и ожидание подтверждения
    pending_apply_prog = None
    pending_confirm_from = None
    pending_confirm_to = None

    # Текущая программа
    try:
        current_prog = client.get_current_program()
    except Exception:
        current_prog = 4
    log.info("Initial program: %s", current_prog)

    try:
        while True:
            # 0) свободное место — решаем, сохранять ли кадры
            saving_allowed = False
            if saving_globally_enabled:
                ok, free = space_ok(save_root, min_free_gb)
                if ok:
                    saving_allowed = True
                else:
                    log.warning("Snapshots PAUSED: low disk space at %s (free: %s < %.2f GB).",
                                save_root.resolve(), format_bytes(free), min_free_gb)

            day_dir = today_dir(save_root) if saving_allowed else None
            if day_dir is not None:
                day_dir.mkdir(parents=True, exist_ok=True)

            # 1) мониторинг
            st = client.get_phase_status()
            phase = int(st.get("phase", 1))
            time_left = int(st.get("time_left", 0))
            prog = int(st.get("program", current_prog))
            if 1 <= prog <= 16:
                current_prog = prog

            # Guard: наблюдаем подтверждение/таймаут
            guard_state = engine.guard_observe(st)
            if guard_state == 'confirmed':
                # фиксируем смену в отдельный файл (CSV): timestamp,from,to
                if pending_confirm_from is not None and pending_confirm_to is not None:
                    proglog.info("%d,%d", pending_confirm_from, pending_confirm_to)
                log.info("Program switch confirmed by monitoring")
                pending_confirm_from = None
                pending_confirm_to = None
            elif guard_state == 'failed':
                log.warning("Program switch timed out (no confirmation)")
                pending_confirm_from = None
                pending_confirm_to = None

            # 2) переходы фаз (фиксируем начало красного)
            if prev_phase is not None:
                if prev_phase == 1 and phase == 2:
                    # main стал красным
                    main_red_t0 = time.time()
                    cycle_id += 1  # считаем начало "цикла" здесь — согласованно используем ниже
                    log.info("[PHASE] 1->2  (main RED start)")
                elif prev_phase == 2 and phase == 1:
                    # side стал красным
                    side_red_t0 = time.time()
                    log.info("[PHASE] 2->1  (side RED start)")
            prev_phase = phase

            # 3) съём очередей «через N секунд посе начала красного»
            now = time.time()

            # main: замерить только когда main на красном (фаза 2)
            if (
                phase == 2 and
                main_red_t0 is not None and
                (now - main_red_t0) >= red_delay and
                main_sampled_cycle != cycle_id
            ):
                q = count_direction(detector, vc, main_cams, shots_per_probe, log,
                                    (day_dir / "main") if day_dir else None)
                last_q_main = q
                main_sampled_cycle = cycle_id
                main_red_t0 = None  # сброс, чтобы не пересчитать повторно
                log.info("[MEAS] main queue=%d (prog=%s, +%ss after red)", q, current_prog, red_delay)

            # side: замерить только когда side на красном (фаза 1)
            if (
                phase == 1 and
                side_red_t0 is not None and
                (now - side_red_t0) >= red_delay and
                side_sampled_cycle != cycle_id
            ):
                q = count_direction(detector, vc, side_cams, shots_per_probe, log,
                                    (day_dir / "side") if day_dir else None)
                last_q_side = q
                side_sampled_cycle = cycle_id
                side_red_t0 = None  # сброс
                log.info("[MEAS] side queue=%d (prog=%s, +%ss after red)", q, current_prog, red_delay)

            # 4A) ARM: обе оценки в этом цикле готовы — принимаем решение
            if (
                pending_apply_prog is None and
                main_sampled_cycle == cycle_id and
                side_sampled_cycle == cycle_id
            ):
                new_prog = engine.decide(current_prog, last_q_main, last_q_side)
                if new_prog is not None and new_prog != current_prog and engine.guard_allow():
                    pending_apply_prog = new_prog
                    log.info("Program change ARMED: %s -> %s (will send at time_left<=%ss)",
                             current_prog, pending_apply_prog, lead_sec)

            # 4B) APPLY: отправить в окно перед концом текущей фазы
            if pending_apply_prog is not None and time_left <= lead_sec and engine.guard_allow():
                ok = client.set_program(pending_apply_prog)
                if ok:
                    log.info("Program change REQUESTED: %s -> %s (awaiting confirmation)",
                             current_prog, pending_apply_prog)
                    pending_confirm_from = current_prog
                    pending_confirm_to = pending_apply_prog
                    engine.guard_start(pending_apply_prog)
                    pending_apply_prog = None
                else:
                    log.error("Program change request failed to send")

            time.sleep(max(0.05, float(cfg.get("analysis", "poll_period_sec", default=0.5))))
    except KeyboardInterrupt:
        log.info("Shutting down service")
    finally:
        client.close()


if __name__ == "__main__":
    main()
