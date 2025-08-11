FROM ultralytics/ultralytics:latest-jetson-jetpack4

WORKDIR /app
COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 5000 8000 8554 22
