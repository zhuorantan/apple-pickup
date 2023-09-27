FROM python

WORKDIR /app

COPY requirements.txt /app/

RUN pip install -r requirements.txt

COPY *.py /app/

ENV APPLE_PICKUP_DATA_DIR=/data
RUN mkdir -p /data

ENTRYPOINT ["python", "apple-pickup.py"]
