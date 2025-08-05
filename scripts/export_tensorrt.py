import argparse
import shutil
from ultralytics import YOLO


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a YOLO model to TensorRT engine format")
    parser.add_argument(
        "--model", default="yolo11s.pt",
        help="Path to the source .pt model")
    parser.add_argument(
        "--imgsz", type=int, default=640,
        help="Image size used during export")
    parser.add_argument(
        "--device", default='0',
        help="Device ID to use for export")
    parser.add_argument(
        "--output", default="models/yolo11s.engine",
        help="Destination path for the TensorRT engine")
    args = parser.parse_args()

    yolo = YOLO(args.model)
    engine_path = yolo.export(format="engine", imgsz=args.imgsz,
                              device=args.device, half=True)
    shutil.move(engine_path, args.output)
    print(f"TensorRT engine saved to {args.output}")


if __name__ == "__main__":
    main()
