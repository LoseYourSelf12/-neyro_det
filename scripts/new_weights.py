import sys
from pathlib import Path
import torch

# ====== НАСТРОЙКИ ======
ULTRA_PT   = r"E:\Programming\!-neyro_det\runs\detect\test_6\weights\yolov5s.pt" 
Y5_REPO    = r"E:\Programming\!-neyro_det\yolov5"
ARCH_YAML  = r"E:\Programming\!-neyro_det\yolov5\models/yolov5s.yaml"
NC         = 3 
OUT_PT     = r"E:\Programming\!-neyro_det\models\yolov5s.pt" 
# =======================

def main():
    y5repo = Path(Y5_REPO).resolve()
    if not y5repo.exists():
        print(f"[ERR] YOLOv5 repo not found at: {y5repo}", file=sys.stderr)
        sys.exit(1)

    sys.path.insert(0, str(y5repo))

    try:
        from ultralytics import YOLO
    except Exception as e:
        print("[ERR] Не найден пакет 'ultralytics'. Установите его на МАШИНЕ КОНВЕРТАЦИИ: pip install ultralytics", file=sys.stderr)
        raise

    m_ultra = YOLO(ULTRA_PT)
    sd = m_ultra.model.state_dict()

    if sd and all(k.startswith("model.") for k in sd.keys()):
        sd = {k.replace("model.", "", 1): v for k, v in sd.items()}
    if sd and all(k.startswith("module.") for k in sd.keys()):
        sd = {k.replace("module.", "", 1): v for k, v in sd.items()}

    from models.yolo import Model
    from utils.general import check_yaml

    arch_yaml = check_yaml(str((y5repo / ARCH_YAML).resolve()))
    model_old = Model(arch_yaml, ch=3, nc=NC)

    missing, unexpected = model_old.load_state_dict(sd, strict=False)
    print("[INFO] Missing keys:", len(missing))
    if missing:
        print(missing[:20], "..." if len(missing) > 20 else "")
    print("[INFO] Unexpected keys:", len(unexpected))
    if unexpected:
        print(unexpected[:20], "..." if len(unexpected) > 20 else "")

    model_old.float().eval()

    ckpt = {
        "model": model_old,
        "epoch": -1,
        "best_fitness": 0.0,
    }
    torch.save(ckpt, OUT_PT)
    print(f"[OK] Saved: {OUT_PT}")

if __name__ == "__main__":
    main()
