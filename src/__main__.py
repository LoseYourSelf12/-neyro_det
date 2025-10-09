import logging
import time
from pathlib import Path
import shutil
from datetime import datetime

import cv2

from .config import Config
from .logger import setup_logging
from .analyzer import CountsAggregator
from .video_capture import VideoCapture
from .detector import Detector
from .controller_client import ControllerClient
from .decision import DecisionEngine

# ---------- утилиты сохранения кадров ----------
def _today_dir(root: Path) -> Path:
    return root / datetime.now().strftime("%Y%m%d")

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} PB"

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

def _save_frame(path: Path, img) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)

# ---------- съём очереди ----------
def _count_direction(detector, vc: VideoCapture, cam_ids, shots: int, log: logging.Logger, save_dir: Path | None):
    agg = CountsAggregator()
    for _ in range(max(1, shots)):
        total = 0
        for cam_id in cam_ids:
            frame = vc.read(cam_id)
            if frame is None:
                log.warning("Frame for camera %s unavailable", cam_id)
                continue
            dets = detector.predict(frame) if detector else []
            total += sum(1 for d in dets if d["name"] in ("car", "bus", "truck"))

            if save_dir is not None:
                ts = datetime.now().strftime("%H%M%S_%f")[:-3]
                img = frame.copy()
                # можно положить аннотирование при желании
                _save_frame(save_dir / f"cam_{cam_id}" / f"{ts}.jpg", img)
        agg.add(total)
    return int(round(agg.average()))

# ---------- главный сервис ----------
def main() -> None:
    cfg = Config()                       # загружаем default.json
    setup_logging(cfg)
    log = logging.getLogger("service")

    # контроллер
    client = ControllerClient(cfg)       # слушаем порт, ждём p/!00/W и т.д. :contentReference[oaicite:6]{index=6}
    log.info("Controller connected and ready")

    # пар.rешателя
    engine = DecisionEngine(cfg)

    # детектор и камеры
    detector = None
    try:
        detector = Detector(cfg)
        log.info("Detector model loaded")
    except Exception as e:
        log.warning("Detector unavailable: %s", e)
    vc = VideoCapture(cfg)

    # сохранение кадров — как в вашем коде (директория/порог)
    save_root = Path(cfg.get("snapshots", "save_dir", default="/media/MYUSB/neyro_det"))
    min_free_gb = float(cfg.get("snapshots", "min_free_gb", default=1.0))
    saving_enabled = _is_saving_allowed(save_root, min_free_gb, log)
    if saving_enabled:
        free = shutil.disk_usage(save_root).free
        log.info("Snapshots enabled at %s (free %s)", save_root, _fmt_bytes(free))
    else:
        log.info("Snapshots disabled")

    # настройки анализа
    shots_per_probe = int(cfg.get("analysis", "shots_per_phase", default=1))
    poll_period = float(cfg.get("analysis", "poll_period_sec", default=0.5))
    sample_window = float(cfg.get("analysis", "sample_window_sec", default=3.0))

    # сопоставление направлений с камерами
    main_cams = cfg.get("analysis", "main_cameras", default=["1"]) or ["1"]
    side_cams = cfg.get("analysis", "side_cameras", default=["2", "3"]) or ["2", "3"]

    # состояние цикла
    prev_phase: int | None = None
    cycle_id = 0
    sampled_main_cycle = -1
    sampled_side_cycle = -1
    last_q_main = 0
    last_q_side = 0

    # текущая программа
    try:
        current_prog = client.get_current_program()   # b02 + парсинг xHEX :contentReference[oaicite:7]{index=7} :contentReference[oaicite:8]{index=8}
    except Exception:
        current_prog = 4
    log.info("Initial program: %s", current_prog)

    try:
        while True:
            # 1) читаем мониторинг
            st = client.get_phase_status()  # {'program','phase','time_left',...} :contentReference[oaicite:9]{index=9} :contentReference[oaicite:10]{index=10}
            phase = int(st.get("phase", 1))
            time_left = int(st.get("time_left", 0))
            prog = int(st.get("program", current_prog))
            # фиксируем текущую программу, если пришла валидная (1..16)
            if 1 <= prog <= 16:
                current_prog = prog

            # 2) инкремент цикла на переходе 2->1
            if prev_phase is not None and prev_phase == 2 and phase == 1:
                cycle_id += 1
                # если обе очереди были сняты в предыдущем цикле — решение и, при необходимости, gNN
                if sampled_main_cycle == cycle_id - 1 and sampled_side_cycle == cycle_id - 1:
                    new_prog = engine.decide(current_prog, last_q_main, last_q_side)
                    if new_prog != current_prog and new_prog >= 4:
                        # print('!!!!set new prog', new_prog)
                        ok = client.set_program(new_prog)  # gNN
                        # ok = True
                        if ok:
                            current_prog = new_prog
                # сбросим семплы "на следующий круг"
                # (если что-то не успели снять — просто перезапишем в новом цикле)
                last_q_main = last_q_main
                last_q_side = last_q_side

            prev_phase = phase

            # 3) съём очереди в нужное окно перед концом зелёного
            day_dir = _today_dir(save_root) if saving_enabled else None

            if phase == 1 and time_left <= sample_window and sampled_main_cycle != cycle_id:
                q = _count_direction(detector, vc, main_cams, shots_per_probe, log,
                                     day_dir / "main" if day_dir else None)
                last_q_main = q
                sampled_main_cycle = cycle_id
                log.info("[MEAS] main queue=%d (prog=%s, time_left=%ss)", q, current_prog, time_left)

            if phase == 2 and time_left <= sample_window and sampled_side_cycle != cycle_id:
                q = _count_direction(detector, vc, side_cams, shots_per_probe, log,
                                     day_dir / "side" if day_dir else None)
                last_q_side = q
                sampled_side_cycle = cycle_id
                log.info("[MEAS] side queue=%d (prog=%s, time_left=%ss)", q, current_prog, time_left)

            time.sleep(max(0.05, poll_period))
    except KeyboardInterrupt:
        log.info("Shutting down service")
    finally:
        client.close()

if __name__ == "__main__":
    main()
