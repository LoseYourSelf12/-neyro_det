import logging
from pathlib import Path
import shutil
from datetime import datetime
import time
import traceback

import cv2

from .config import Config
from .logger import setup_logging
from .analyzer import CountsAggregator
from .video_capture import VideoCapture
from .detector import Detector
from .controller_client import ControllerClient
from .decision import DecisionEngine
from logging.handlers import RotatingFileHandler

# ---------- цвета BGR для классов ----------
CLASS_COLOR = {
    "car":   (0, 255, 0),     # зелёный
    "truck": (0, 165, 255),   # оранжевый
    "bus":   (255, 0, 0),     # синий
}

# ---------- утилиты сохранения кадров ----------
def _today_dir(root: Path) -> Path:
    return root / datetime.now().strftime("%Y%m%d")

def _timestamp_name(ext: str = "jpg") -> str:
    return datetime.now().strftime("%H%M%S_%f")[:-3] + "." + ext

def _unique_path(dir_, fname):
    p = dir_ / fname
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    k = 1
    while True:
        cand = dir_ / ("%s_%d%s" % (stem, k, suf))
        if not cand.exists():
            return cand
        k += 1

def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%d %s" % (n, unit)
        n //= 1024
    return "%d PB" % n

def _is_saving_allowed(root: Path, min_free_gb: float, log: logging.Logger) -> bool:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        usage = shutil.disk_usage(root)
        return usage.free >= int(min_free_gb * (1024 ** 3))
    except Exception as e:
        log.error("Snapshots disabled: %s", e)
        return False

def _space_ok(root: Path, min_free_gb: float):
    try:
        usage = shutil.disk_usage(root)
        return (usage.free >= int(min_free_gb * (1024 ** 3))), usage.free
    except Exception:
        return False, 0

def _save_frame(path: Path, img) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return bool(cv2.imwrite(str(path), img))
    except Exception:
        return False

def _draw_detections(img, detections):
    annotated = img.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.5
    thickness = 2

    for det in detections:
        try:
            if "xyxy" in det:
                x1, y1, x2, y2 = det["xyxy"]
            elif "bbox" in det:
                x1, y1, x2, y2 = det["bbox"]
            elif "xywh" in det:
                x, y, w, h = det["xywh"]
                x1, y1, x2, y2 = int(x - w/2), int(y - h/2), int(x + w/2), int(y + h/2)
            else:
                continue

            name = det.get("name", "obj")
            conf = det.get("conf")
            color = CLASS_COLOR.get(name, (0, 255, 255))

            cv2.rectangle(annotated, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
            label = "%s%s" % (name, (" %.2f" % float(conf)) if conf is not None else "")
            (tw, th), _ = cv2.getTextSize(label, font, font_scale, thickness)
            cv2.rectangle(annotated, (int(x1), int(y1) - th - 6), (int(x1) + tw + 4, int(y1)), color, -1)
            cv2.putText(annotated, label, (int(x1) + 2, int(y1) - 4),
                        font, font_scale, (255, 255, 255), 1, cv2.LINE_AA)
        except Exception:
            continue

    return annotated

# ---------- логгер смен программ (отдельный файл) ----------
def _setup_progchange_logger(cfg: Config) -> logging.Logger:
    path = cfg.get("logging", "program_changes_file", default="logs/program_changes.log")
    level_name = cfg.get("logging", "level", default="INFO")
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    max_bytes = cfg.get("logging", "max_bytes", default=10_485_760)
    backup_count = cfg.get("logging", "backup_count", default=5)

    logger = logging.getLogger("progchange")
    logger.setLevel(level)
    logger.propagate = False  # ВАЖНО: чтобы не шло в консоль/общий файл

    # не плодим хэндлеры при повторных запусках
    if not logger.handlers:
        log_dir = Path(path).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=int(max_bytes), backupCount=int(backup_count), encoding="utf-8")
        # CSV: timestamp,from,to
        fmt = logging.Formatter("%(asctime)s,%(message)s", datefmt="%Y-%m-%d %H:%M:%S.%f")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        logger.addHandler(fh)
    return logger

# ---------- съём очереди ----------
def _count_direction(detector, vc, cam_ids, shots, log, save_dir):
    agg = CountsAggregator()
    for _ in range(max(1, int(shots))):
        total = 0
        for cam_id in cam_ids:
            frame = vc.read(cam_id)
            if frame is None:
                log.warning("Frame for camera %s is unavailable, skipping detection", cam_id)
                continue

            dets = detector.predict(frame) if detector else []
            count = sum(1 for d in dets if d.get("name") in ("car", "bus", "truck"))
            total += count

            out_img = _draw_detections(frame, dets) if dets else frame

            if save_dir is not None:
                cam_dir = save_dir / ("cam_%s" % cam_id)
                cam_dir.mkdir(parents=True, exist_ok=True)
                fname = _timestamp_name("jpg")
                out_path = _unique_path(cam_dir, fname)
                if _save_frame(out_path, out_img):
                    logging.getLogger("service").info("Saved snapshot for camera %s: %s", cam_id, out_path.resolve())
                else:
                    logging.getLogger("service").error("Failed to save frame for camera %s to %s", cam_id, out_path)

        agg.add(total)
    return int(round(agg.average()))

# ---------- главный сервис ----------
def main():
    cfg = Config()
    setup_logging(cfg)
    log = logging.getLogger("service")
    proglog = _setup_progchange_logger(cfg)  # отдельный файл смен программ

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
    saving_globally_enabled = _is_saving_allowed(save_root, min_free_gb, log)
    if saving_globally_enabled:
        free = shutil.disk_usage(save_root).free
        log.info("Snapshots ENABLED at: %s (free: %s, threshold: %.2f GB)",
                 save_root.resolve(), _fmt_bytes(free), min_free_gb)
    else:
        log.error("Snapshots DISABLED: saving will be skipped (no fallback).")

    # Настройки анализа
    shots_per_probe = int(cfg.get("analysis", "shots_per_phase", default=1))
    poll_period = float(cfg.get("analysis", "poll_period_sec", default=0.5))
    # sample_window больше не нужен — детект делаем ПОСЛЕ начала красного
    red_delay = float(cfg.get("analysis", "red_sample_delay_sec", default=5.0))

    # Окно отправки и карты камер
    lead_sec = int(cfg.get("controller", "traffic_phase_lead_sec", default=2))
    main_cams = cfg.get("analysis", "main_cameras", default=["1"]) or ["1"]
    side_cams = cfg.get("analysis", "side_cameras", default=["2", "3"]) or ["2", "3"]

    # Состояние цикла
    prev_phase = None
    cycle_id = 0

    # «когда начался красный» для каждого направления
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
                ok, free = _space_ok(save_root, min_free_gb)
                if ok:
                    saving_allowed = True
                else:
                    log.warning("Snapshots PAUSED: low disk space at %s (free: %s < %.2f GB).",
                                save_root.resolve(), _fmt_bytes(free), min_free_gb)

            day_dir = _today_dir(save_root) if saving_allowed else None
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
                    cycle_id += 1  # переход 1->2 считаем началом нового «замера цикла»
                elif prev_phase == 2 and phase == 1:
                    # side стал красным
                    side_red_t0 = time.time()
                    # cycle_id инкрементируем на переходе 2->1? — уже сделали выше на 1->2,
                    # оставим один инкремент на цикл
            prev_phase = phase

            # 3) съём очередей «через N секунд после начала красного»
            now = time.time()

            # main: замерить после перехода 1->2
            if main_red_t0 is not None and (now - main_red_t0) >= red_delay and main_sampled_cycle != cycle_id:
                q = _count_direction(detector, vc, main_cams, shots_per_probe, log,
                                     (day_dir / "main") if day_dir else None)
                last_q_main = q
                main_sampled_cycle = cycle_id
                log.info("[MEAS] main queue=%d (prog=%s, +%ss after red)", q, current_prog, red_delay)

            # side: замерить после перехода 2->1
            if side_red_t0 is not None and (now - side_red_t0) >= red_delay and side_sampled_cycle != cycle_id:
                q = _count_direction(detector, vc, side_cams, shots_per_probe, log,
                                     (day_dir / "side") if day_dir else None)
                last_q_side = q
                side_sampled_cycle = cycle_id
                log.info("[MEAS] side queue=%d (prog=%s, +%ss after red)", q, current_prog, red_delay)

            # 4A) ARM: обе оценки в этом цикле готовы — принимаем решение
            if (pending_apply_prog is None and
                main_sampled_cycle == cycle_id and side_sampled_cycle == cycle_id):
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
                    # для отдельного файла фиксируем «что ожидали подтвердить»
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
