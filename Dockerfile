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

# Copy scripts
COPY ha_discovery.py /app/ha_discovery.py

# Create supervisor config
RUN echo "[supervisord]\nnodaemon=true\n\n[program:powertagd]\ncommand=/usr/local/bin/powertagd -o mqtt --host %(ENV_MQTT_HOST)s --port %(ENV_MQTT_PORT)s -d %(ENV_SERIAL_DEV)s %(ENV_EXTRA_ARGS)s\nstdout_logfile=/dev/stdout\nstdout_logfile_maxbytes=0\nstderr_logfile=/dev/stderr\nstderr_logfile_maxbytes=0\n\n[program:discovery]\ncommand=python3 /app/ha_discovery.py\nstdout_logfile=/dev/stdout\nstdout_logfile_maxbytes=0\nstderr_logfile=/dev/stderr\nstderr_logfile_maxbytes=0" > /etc/supervisor/conf.d/supervisord.conf

# Env vars defaults
ENV MQTT_HOST=localhost
ENV MQTT_PORT=1883
ENV SERIAL_DEV=/dev/ttyACM0
ENV EXTRA_ARGS=""

CMD ["/usr/bin/supervisord"]
