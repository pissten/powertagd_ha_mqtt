import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

# Configuration
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER", None)
MQTT_PASS = os.getenv("MQTT_PASSWORD", None)
DISCOVERY_PREFIX = os.getenv("DISCOVERY_PREFIX", "homeassistant")
DATA_DIR = os.getenv("DATA_DIR", "/data")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8092"))

sys.stdout.reconfigure(line_buffering=True)  # Force unbuffered output for logs
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

SENSOR_CONFIG = {
    "total_power_active": {
        "device_class": "power", "unit": "W", "state_class": "measurement", "name": "Active Power", "topic": "electrical"
    },
    "total_power_apparent": {
        "device_class": "apparent_power", "unit": "VA", "state_class": "measurement", "name": "Apparent Power", "topic": "electrical"
    },
    "voltage_p1": {
        "device_class": "voltage", "unit": "V", "state_class": "measurement", "name": "Voltage", "topic": "electrical"
    },
    "current_p1": {
        "device_class": "current", "unit": "A", "state_class": "measurement", "name": "Current", "topic": "electrical"
    },
    "power_factor": {
        "device_class": "power_factor", "unit": "%", "state_class": "measurement", "name": "Power Factor", "topic": "metering"
    },
    "total_energy_export": {
        "device_class": "energy", "unit": "kWh", "state_class": "total_increasing", "name": "Energy Export", "topic": "metering"
    },
    "total_energy_import": {
        "device_class": "energy", "unit": "kWh", "state_class": "total_increasing", "name": "Energy Import", "topic": "metering"
    },
    "lqi": {
        "name": "Link Quality",
        "topic": "electrical",  # LQI is sent with all updates, pick one
        "unit": "lqi",
        "device_class": None,  # LQI has no standard class, maybe "signal_strength" if dBm
        "state_class": "measurement",
        "icon": "mdi:signal",
        "entity_category": "diagnostic"
    },
    # Phase 2
    "voltage_p2": {
        "device_class": "voltage", "unit": "V", "state_class": "measurement", "name": "Voltage L2", "topic": "electrical"
    },
    "current_p2": {
        "device_class": "current", "unit": "A", "state_class": "measurement", "name": "Current L2", "topic": "electrical"
    },
    "power_p2_active": {
        "device_class": "power", "unit": "W", "state_class": "measurement", "name": "Active Power L2", "topic": "electrical"
    },
    # Phase 3
    "voltage_p3": {
        "device_class": "voltage", "unit": "V", "state_class": "measurement", "name": "Voltage L3", "topic": "electrical"
    },
    "current_p3": {
        "device_class": "current", "unit": "A", "state_class": "measurement", "name": "Current L3", "topic": "electrical"
    },
    "power_p3_active": {
        "device_class": "power", "unit": "W", "state_class": "measurement", "name": "Active Power L3", "topic": "electrical"
    }
}

LABELS = {
    "total_power_active": "Effekt",
    "total_power_apparent": "Tilsynelatende effekt",
    "voltage_p1": "Spenning",
    "current_p1": "Strøm",
    "power_factor": "Effektfaktor",
    "total_energy_export": "Energi ut",
    "total_energy_import": "Energi inn",
    "lqi": "Signal",
    "voltage_p2": "Spenning L2",
    "current_p2": "Strøm L2",
    "power_p2_active": "Effekt L2",
    "voltage_p3": "Spenning L3",
    "current_p3": "Strøm L3",
    "power_p3_active": "Effekt L3",
}

# Control Buttons
CONTROL_BUTTONS = {
    "scan": {
        "name": "Start Scan",
        "command_topic": "powertag/cmd/scan",
        "icon": "mdi:wifi-refresh"
    },
    "pair": {
        "name": "Start Pairing",
        "command_topic": "powertag/cmd/pair",
        "icon": "mdi:link-plus"
    }
}

# Configuration for the Bridge Device itself (Controller)
BRIDGE_INFO = {
    "identifiers": ["powertagd_bridge"],
    "name": "PowerTag Zigbee Gateway",
    "manufacturer": "DIY",
    "model": "PowerTagD Container",
    "sw_version": "2.1"
}

# Track configured devices to avoid spamming discovery stats
configured_devices = set()
configured_bridge = False
device_metadata = {}  # {device_id: {"fw_ver": "...", "model": "...", "serial": "..."}}
config = {"devices": {}}
state = {}
scan_results = []
mqtt_connected = False
mqtt_client = None
last_state_save = 0.0
lock = threading.Lock()

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def is_device_id(value):
    return bool(re.fullmatch(r"[0-9a-f]{8}", value or ""))


def slugify(name, device_id):
    text = (name or "").strip().lower()
    text = text.replace("æ", "ae").replace("ø", "o").replace("å", "a")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not text:
        return device_id
    if text in ("cmd", "bridge", "scan_result") or is_device_id(text):
        text = "n-" + text
    return text[:48]


def topic_key(device_id, name, publish_as):
    if publish_as != "name":
        return device_id
    base = slugify(name, device_id)
    for other_id, other in config["devices"].items():
        if other_id == device_id or other.get("publish_as") != "name":
            continue
        if slugify(other.get("name", ""), other_id) == base:
            return f"{base}-{device_id[:4]}"
    return base


def device_settings(device_id):
    saved = config["devices"].get(device_id, {})
    name = str(saved.get("name") or "").strip()
    publish_as = saved.get("publish_as") if saved.get("publish_as") in ("id", "name") else "id"
    return name, publish_as, topic_key(device_id, name, publish_as)


def atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def load_config():
    global config
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict) and isinstance(loaded.get("devices"), dict):
            config = loaded
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"Could not read {CONFIG_PATH}: {exc}", file=sys.stderr)


def load_state():
    global state
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            state = loaded
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"Could not read {STATE_PATH}: {exc}", file=sys.stderr)

    for device_id, item in state.items():
        basic = item.get("clusters", {}).get("basic", {})
        if not isinstance(basic, dict):
            continue
        meta = {}
        for key in ("fw_ver", "model", "serial"):
            if key in basic:
                meta[key] = basic[key]
        if meta:
            device_metadata[device_id] = meta


def save_config():
    try:
        atomic_write(CONFIG_PATH, config)
    except Exception as exc:
        print(f"Could not write {CONFIG_PATH}: {exc}", file=sys.stderr)


def save_state(force=False):
    global last_state_save
    now = time.time()
    if not force and now - last_state_save < 2:
        return
    last_state_save = now
    try:
        atomic_write(STATE_PATH, state)
    except Exception as exc:
        print(f"Could not write {STATE_PATH}: {exc}", file=sys.stderr)


def known_device_ids():
    found = set(state) | set(config["devices"])
    return sorted(device_id for device_id in found if is_device_id(device_id))


def device_view(device_id):
    name, publish_as, slug = device_settings(device_id)
    item = state.get(device_id, {})
    clusters = item.get("clusters", {})
    values = {}
    if isinstance(clusters, dict):
        for payload in clusters.values():
            if isinstance(payload, dict):
                values.update(payload)
    meta = device_metadata.get(device_id, {})
    entities = []
    for metric, sensor in SENSOR_CONFIG.items():
        entities.append({
            "metric": metric,
            "name": sensor["name"],
            "label": LABELS.get(metric, sensor["name"]),
            "unit": sensor["unit"],
            "topic": f"powertag/{slug}/{sensor['topic']}",
            "value": values.get(metric),
        })
    return {
        "id": device_id,
        "name": name,
        "publish_as": publish_as,
        "slug": slug,
        "model": meta.get("model"),
        "serial": meta.get("serial"),
        "fw_ver": meta.get("fw_ver"),
        "last_seen": item.get("last_seen"),
        "values": values,
        "entities": entities,
    }


def snapshot():
    with lock:
        devices = [device_view(device_id) for device_id in known_device_ids()]
        return {
            "mqtt_connected": mqtt_connected,
            "devices": devices,
            "scan": list(scan_results),
        }


def ensure_bridge_configured(client):
    global configured_bridge
    if configured_bridge:
        return

    print("Configuring Bridge Controls...")

    for btn_id, btn_config in CONTROL_BUTTONS.items():
        unique_id = f"powertagd_bridge_{btn_id}"
        discovery_topic = f"{DISCOVERY_PREFIX}/button/powertagd_bridge/{btn_id}/config"

        payload = {
            "name": btn_config["name"],
            "unique_id": unique_id,
            "device": BRIDGE_INFO,
            "command_topic": btn_config["command_topic"],
            "payload_press": "true",  # We don't care about payload content, just the topic trigger
            "icon": btn_config["icon"]
        }
        client.publish(discovery_topic, json.dumps(payload), retain=True)

    configured_bridge = True


def ensure_device_configured(client, device_id, force=False):
    if device_id in configured_devices and not force:
        return

    if force and device_id in configured_devices:
        print(f"Refreshing discovery for {device_id}", flush=True)
    else:
        print(f"Discovered new device: {device_id}", flush=True)

    with lock:
        name, _publish_as, slug = device_settings(device_id)
        meta = dict(device_metadata.get(device_id, {}))

    device_info = {
        "identifiers": [f"powertag_{device_id}"],
        "name": name or f"PowerTag {device_id}",
        "manufacturer": "Schneider Electric",
        "model": meta.get("model", "PowerTag Zigbee"),
        "sw_version": meta.get("fw_ver", "Unknown"),
        "serial_number": meta.get("serial", "Unknown"),
        "via_device": "powertagd_bridge"
    }

    for metric, sensor in SENSOR_CONFIG.items():
        unique_id = f"powertag_{device_id}_{metric}"
        discovery_topic = f"{DISCOVERY_PREFIX}/sensor/powertag_{device_id}/{metric}/config"

        payload = {
            "name": sensor["name"],
            "unique_id": unique_id,
            "device": device_info,
            "state_topic": f"powertag/{slug}/{sensor['topic']}",
            "unit_of_measurement": sensor["unit"],
            "device_class": sensor["device_class"],
            "state_class": sensor["state_class"],
            "value_template": f"{{{{ value_json.{metric} }}}}"
        }

        if sensor.get("icon"):
            payload["icon"] = sensor["icon"]

        if sensor.get("entity_category"):
            payload["entity_category"] = sensor["entity_category"]

        # Publish discovery config with Retain=True
        print(f"Publishing discovery for {metric}", flush=True)
        client.publish(discovery_topic, json.dumps(payload), retain=True)

    configured_devices.add(device_id)


def publish_alias(client, device_id, cluster, payload):
    with lock:
        _name, publish_as, slug = device_settings(device_id)
    if publish_as != "name" or slug == device_id:
        return
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    client.publish(f"powertag/{slug}/{cluster}", body, qos=1, retain=True)


def clear_alias(client, slug):
    if not slug or is_device_id(slug):
        return
    for cluster in ("basic", "electrical", "metering", "other"):
        client.publish(f"powertag/{slug}/{cluster}", b"", qos=1, retain=True)


def republish_known(client):
    global configured_bridge
    with lock:
        configured_bridge = False
        copies = {}
        for device_id in known_device_ids():
            clusters = state.get(device_id, {}).get("clusters", {})
            copies[device_id] = dict(clusters) if isinstance(clusters, dict) else {}
    ensure_bridge_configured(client)
    for device_id, clusters in copies.items():
        ensure_device_configured(client, device_id, force=True)
        for cluster, payload in clusters.items():
            publish_alias(client, device_id, cluster, payload)


def remember_measurement(device_id, cluster, payload, retained):
    with lock:
        item = state.setdefault(device_id, {"clusters": {}})
        item.setdefault("clusters", {})[cluster] = payload
        if not retained:
            item["last_seen"] = time.time()
        save_state()


def clean_text(value):
    text = str(value)
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text


def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    print(f"Connected with result code {rc}")
    if rc != 0:
        mqtt_connected = False
        return
    mqtt_connected = True
    # Subscribe to all powertag topics, including scan results.
    client.subscribe("powertag/#")
    print("Subscribed to powertag/#", flush=True)
    republish_known(client)


def on_disconnect(client, userdata, rc):
    global mqtt_connected
    mqtt_connected = False
    if rc != 0:
        print(f"MQTT disconnected ({rc}), retrying", flush=True)


def on_message(client, userdata, msg):
    # Expected topic: powertag/<device_id>/<cluster>
    if DEBUG:
        print(f"Rx: {msg.topic}", flush=True)
    try:
        if msg.topic == "powertag/scan_result":
            try:
                payload = json.loads(msg.payload.decode())
            except Exception:
                return
            with lock:
                if not msg.retain:
                    channel = payload.get("channel")
                    scan_results[:] = [row for row in scan_results if row.get("channel") != channel]
                    scan_results.append(payload)
                elif not scan_results:
                    scan_results.append(payload)
            return

        parts = msg.topic.split("/")
        if len(parts) != 3:
            return

        device_id = parts[1].lower()
        cluster = parts[2]

        # Ignore special command topics, bridge status and name aliases.
        if not is_device_id(device_id):
            return

        # Parse payload
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        remember_measurement(device_id, cluster, payload, bool(msg.retain))

        # Capture metadata from 'basic' cluster
        if cluster == "basic":
            with lock:
                if device_id not in device_metadata:
                    device_metadata[device_id] = {}

                updated_meta = False
                for key in ["fw_ver", "model", "serial"]:
                    if key in payload and payload[key] != device_metadata[device_id].get(key):
                        device_metadata[device_id][key] = clean_text(payload[key])
                        updated_meta = True

            if device_id not in configured_devices or updated_meta:
                ensure_device_configured(client, device_id, force=updated_meta)

        # Trigger discovery for other clusters too if new device
        elif device_id not in configured_devices:
            ensure_device_configured(client, device_id)

        publish_alias(client, device_id, cluster, msg.payload)

    except Exception as e:
        print(f"Error processing message: {e}", file=sys.stderr)


def send_command(topic, payload="true"):
    if mqtt_client is None or not mqtt_connected:
        return False, "MQTT er ikke tilkoblet ennå"
    mqtt_client.publish(topic, payload, qos=1, retain=False)
    return True, "Kommando sendt"


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if DEBUG:
            super().log_message(fmt, *args)

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 8192:
            raise ValueError("For stor forespørsel")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw.decode())
        if not isinstance(data, dict):
            raise ValueError("Ugyldig innhold")
        return data

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            page = os.path.join(WEB_ROOT, "index.html")
            try:
                with open(page, "rb") as handle:
                    body = handle.read()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/devices":
            self._json(200, snapshot())
            return
        if path == "/api/status":
            with lock:
                connected = mqtt_connected
                count = len(known_device_ids())
            self._json(200, {"mqtt_connected": connected, "devices": count})
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/scan":
                with lock:
                    scan_results.clear()
                ok, message = send_command("powertag/cmd/scan")
                self._json(200 if ok else 503, {"ok": ok, "message": "Kanalsøk startet" if ok else message, "error": None if ok else message})
                return
            if path == "/api/pair":
                ok, message = send_command("powertag/cmd/pair")
                text = "Paring startet. Sett PowerTag i paringsmodus nå." if ok else message
                self._json(200 if ok else 503, {"ok": ok, "message": text, "error": None if ok else message})
                return
            identify_prefix = "/api/devices/"
            identify_suffix = "/identify"
            if path.startswith(identify_prefix) and path.endswith(identify_suffix):
                device_id = path[len(identify_prefix):-len(identify_suffix)].lower()
                if not is_device_id(device_id):
                    self._json(400, {"error": "Ukjent enhets-id"})
                    return
                ok, message = send_command("powertag/cmd/identify", device_id)
                text = "Blink er sendt. Lampen blinker i ca. 10 sekunder når taggen svarer." if ok else message
                self._json(200 if ok else 503, {"ok": ok, "message": text, "error": None if ok else message})
                return
            if path == "/api/discovery":
                if mqtt_client is None or not mqtt_connected:
                    self._json(503, {"ok": False, "error": "MQTT er ikke tilkoblet ennå"})
                    return
                republish_known(mqtt_client)
                self._json(200, {"ok": True, "message": "Enhetene er publisert til Home Assistant på nytt"})
                return

            prefix = "/api/devices/"
            if path.startswith(prefix):
                device_id = path[len(prefix):].lower()
                if not is_device_id(device_id):
                    self._json(400, {"error": "Ukjent enhets-id"})
                    return
                body = self._read_json()
                name = str(body.get("name", "")).strip()
                publish_as = body.get("publish_as", "id")
                if publish_as not in ("id", "name"):
                    self._json(400, {"error": "Publisering må være id eller name"})
                    return
                if len(name) > 64:
                    self._json(400, {"error": "Navnet er for langt"})
                    return
                with lock:
                    previous = config["devices"].get(device_id, {})
                    old_slug = topic_key(device_id, previous.get("name", ""), previous.get("publish_as", "id"))
                    config["devices"][device_id] = {"name": name, "publish_as": publish_as}
                    save_config()
                    new_slug = topic_key(device_id, name, publish_as)
                if mqtt_client is not None and mqtt_connected:
                    if old_slug != new_slug:
                        clear_alias(mqtt_client, old_slug)
                    ensure_device_configured(mqtt_client, device_id, force=True)
                    item = state.get(device_id, {})
                    for cluster, payload in item.get("clusters", {}).items():
                        publish_alias(mqtt_client, device_id, cluster, payload)
                self._json(200, {"ok": True, "device": device_view(device_id)})
                return
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            print(f"Web error: {exc}", file=sys.stderr)
            self._json(500, {"error": "Noe gikk galt"})
            return
        self.send_error(404)


def start_web():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as exc:
        print(f"Could not create {DATA_DIR}: {exc}", file=sys.stderr)
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), WebHandler)
    thread = threading.Thread(target=server.serve_forever, name="powertag-web", daemon=True)
    thread.start()
    print(f"Web UI on http://{WEB_HOST}:{WEB_PORT}", flush=True)
    return server


def make_client():
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="powertagd-web")
    return mqtt.Client(client_id="powertagd-web")


def main():
    global mqtt_client
    load_config()
    load_state()
    start_web()

    client = make_client()
    mqtt_client = client
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    if hasattr(client, "reconnect_delay_set"):
        client.reconnect_delay_set(min_delay=2, max_delay=30)

    while True:
        try:
            print(f"Connecting to MQTT Broker {MQTT_HOST}:{MQTT_PORT}...", flush=True)
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            try:
                client.loop_forever(retry_first_connection=True)
            except TypeError as exc:
                if "retry_first_connection" not in str(exc):
                    raise
                client.loop_forever()
        except Exception as exc:
            global mqtt_connected
            mqtt_connected = False
            print(f"MQTT connection failed: {exc}. Retrying in 5s", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
