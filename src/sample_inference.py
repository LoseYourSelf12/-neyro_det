import cv2
from config import Config
from detector import Detector

def main():
    config = Config()
    detector = Detector(config)
    img = cv2.imread('samples/car_test.jpg')
    if img is None:
        print('Error: failed to load samples/car_test.jpg')
        return
    dets = detector.predict(img)
    print(f'Detected {len(dets)} cars:', dets)

if __name__ == '__main__':
    main()