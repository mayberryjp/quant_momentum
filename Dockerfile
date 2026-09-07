FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/New_York

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git vim procps tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

ARG CACHE_BUST=1
RUN git clone https://github.com/mayberryjp/quant_momentum.git .

RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install -e ".[dev]" \
    && python3 -m pip install supervisor

CMD ["supervisord", "-c", "/app/supervisord.conf"]
