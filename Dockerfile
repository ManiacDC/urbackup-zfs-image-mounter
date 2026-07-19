FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

RUN . /etc/os-release \
    && echo "deb http://deb.debian.org/debian ${VERSION_CODENAME} main contrib" > /etc/apt/sources.list.d/contrib.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends zfsutils-linux git \
    && rm -rf /var/lib/apt/lists/* /etc/apt/sources.list.d/contrib.list

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates ./templates

EXPOSE 8000

CMD ["python", "app.py"]
