import json
import os
import sys
import paho.mqtt.client as mqtt

# Configuration
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER", None)
MQTT_PASS = os.getenv("MQTT_PASSWORD", None)
DISCOVERY_PREFIX = os.getenv("DISCOVERY_PREFIX", "homeassistant")

import sys
sys.stdout.reconfigure(line_buffering=True) # Force unbuffered output for logs
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
        "topic": "electrical", # LQI is sent with all updates, pick one
        "unit": "lqi",
        "device_class": None, # LQI has no standard class, maybe "signal_strength" if dBm
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
    "sw_version": "2.0"
}

# Track configured devices to avoid spamming discovery stats
configured_devices = set()
configured_bridge = False
device_metadata = {}  # {device_id: {"fw_ver": "...", "model": "...", "serial": "..."}}

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
            "payload_press": "true", # We don't care about payload content, just the topic trigger
            "icon": btn_config["icon"]
        }
        client.publish(discovery_topic, json.dumps(payload), retain=True)
        
    configured_bridge = True

def on_connect(client, userdata, flags, rc):
    print(f"Connected with result code {rc}")
    # Subscribe to all powertag topics
    client.subscribe("powertag/+/+")
    print("Subscribed to powertag/+/+", flush=True)
    ensure_bridge_configured(client)

def on_message(client, userdata, msg):
    # Expected topic: powertag/<device_id>/<cluster>
    if DEBUG:
        print(f"Rx: {msg.topic}", flush=True)
    try:
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        
        device_id = parts[1]
        cluster = parts[2]
        
        device_id = parts[1]
        cluster = parts[2]

        # Ignore special command topics or bridge status
        if device_id in ["cmd", "bridge"]:
            return
        
        # Parse payload
        try:
            payload = json.loads(msg.payload.decode())
        except:
            return

        # Capture metadata from 'basic' cluster
        if cluster == "basic":
            if device_id not in device_metadata:
                device_metadata[device_id] = {}
                
            updated_meta = False
            for key in ["fw_ver", "model", "serial"]:
                if key in payload and payload[key] != device_metadata[device_id].get(key):
                    val = str(payload[key])
                    # Clean up double-quotes from old/bad retained messages
                    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                        val = val[1:-1]
                        
                    device_metadata[device_id][key] = val
                    updated_meta = True

            if device_id not in configured_devices or updated_meta:
                 ensure_device_configured(client, device_id)
        
        # Trigger discovery for other clusters too if new device
        elif device_id not in configured_devices:
             ensure_device_configured(client, device_id)

    except Exception as e:
        print(f"Error processing message: {e}", file=sys.stderr)

def ensure_device_configured(client, device_id):
    if device_id in configured_devices:
        return

    print(f"Discovered new device: {device_id}", flush=True)
    
    # Get metadata if available
    meta = device_metadata.get(device_id, {})
    
    device_info = {
        "identifiers": [f"powertag_{device_id}"],
        "name": f"PowerTag {device_id}",
        "manufacturer": "Schneider Electric",
        "model": meta.get("model", "PowerTag Zigbee"), 
        "sw_version": meta.get("fw_ver", "Unknown"),
        "serial_number": meta.get("serial", "Unknown"),
        "via_device": "powertagd_bridge"
    }

    for metric, config in SENSOR_CONFIG.items():
        unique_id = f"powertag_{device_id}_{metric}"
        discovery_topic = f"{DISCOVERY_PREFIX}/sensor/powertag_{device_id}/{metric}/config"
        
        payload = {
            "name": config["name"],
            "unique_id": unique_id,
            "device": device_info,
            "state_topic": f"powertag/{device_id}/{config['topic']}",
            "unit_of_measurement": config["unit"],
            "device_class": config["device_class"],
            "state_class": config["state_class"],
            "value_template": f"{{{{ value_json.{metric} }}}}"
        }

        if config.get("icon"):
            payload["icon"] = config["icon"]

        if config.get("entity_category"):
            payload["entity_category"] = config["entity_category"]
        
        # Publish discovery config with Retain=True
        print(f"Publishing discovery for {metric}", flush=True)
        client.publish(discovery_topic, json.dumps(payload), retain=True)

    configured_devices.add(device_id) 

def main():
    client = mqtt.Client()
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to MQTT Broker {MQTT_HOST}:{MQTT_PORT}...")
    client.connect(MQTT_HOST, MQTT_PORT, 60)

    client.loop_forever()

if __name__ == "__main__":
    main()
