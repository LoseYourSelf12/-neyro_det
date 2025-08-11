FROM nvcr.io/nvidia/l4t-pytorch:r32.7.1-pth1.10-py3

WORKDIR /app
COPY requirements.txt /app/requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /var/run/sshd \
    && echo 'root:root' | chpasswd

RUN pip3 install --no-cache-dir -r requirements.txt

RUN git clone https://github.com/ultralytics/yolov5.git /app/yolov5 \
    && sed -i '/torch\|opencv-python\|ultralytics/d' /app/yolov5/requirements.txt \
    && pip3 install --no-cache-dir -r /app/yolov5/requirements.txt

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
COPY . /app

EXPOSE 5000 8000 8554 22

ENTRYPOINT ["/app/docker-entrypoint.sh"]
