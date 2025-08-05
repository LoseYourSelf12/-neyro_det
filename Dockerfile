FROM ultralytics/ultralytics:latest-jetson

WORKDIR /app
COPY . /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /var/run/sshd \
    && echo 'root:root' | chpasswd

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 5000 8000 8554 22

ENTRYPOINT ["/app/docker-entrypoint.sh"]
