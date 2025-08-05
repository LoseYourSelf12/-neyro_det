FROM ultralytics/ultralytics:latest-jetson

WORKDIR /app
COPY requirements.txt /app/requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /var/run/sshd \
    && echo 'root:root' | chpasswd

RUN pip install --no-cache-dir -r requirements.txt

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
COPY . /app

EXPOSE 5000 8000 8554 22

ENTRYPOINT ["/app/docker-entrypoint.sh"]
