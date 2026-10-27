FROM debian:bookworm-slim AS build

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    libmosquitto-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Build powertagd
# CACHE BUST 2026-01-14-RENAME-KEYS
WORKDIR /app/src
RUN make clean && make all

# Runtime Stage
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    libmosquitto1 \
    python3 \
    python3-paho-mqtt \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy binaries
COPY --from=build /app/src/powertagd /usr/local/bin/
COPY --from=build /app/src/powertagctl /usr/local/bin/

# Copy scripts and the device overview
COPY ha_discovery.py /app/ha_discovery.py
COPY web /app/web
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Env vars defaults
ENV MQTT_HOST=localhost
ENV MQTT_PORT=1883
ENV SERIAL_DEV=/dev/ttyACM0
ENV EXTRA_ARGS=""
ENV DATA_DIR=/data
ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=8092

EXPOSE 8092

CMD ["/usr/bin/supervisord"]
