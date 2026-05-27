"""
sensors.py
----------
Reads from the Sense HAT (or emulator) and publishes data to AWS IoT Core
via MQTT. Analysis (spike/trend/prediction) is performed here before publish.

Requirements:
    pip install sense-emu AWSIoTPythonSDK

AWS Setup needed:
    - An IoT Thing created in AWS IoT Core
    - Certificates downloaded into a 'certs/' folder:
        certs/AmazonRootCA1.pem
        certs/device-certificate.pem.crt
        certs/private.pem.key
    - An IoT Policy attached to the certificate allowing iot:Publish
"""

import time
import json
import os
from sense_emu import SenseHat
from AWSIoTPythonSDK.MQTTLib import AWSIoTMQTTClient

# ── AWS IoT Core connection settings ──────────────────────────────────────────
AWS_ENDPOINT   = os.environ.get("AWS_IOT_ENDPOINT", "YOUR_ENDPOINT.iot.REGION.amazonaws.com")
CLIENT_ID      = os.environ.get("IOT_CLIENT_ID",    "smart-env-monitor-pi")
TOPIC          = "smartenv/readings"
CA_PATH        = "certs/AmazonRootCA1.pem"
CERT_PATH      = "certs/device-certificate.pem.crt"
KEY_PATH       = "certs/private.pem.key"
PUBLISH_INTERVAL = 5  # seconds between readings

# ── Analysis state ─────────────────────────────────────────────────────────────
previous_temp  = None
temp_history   = []


def connect_mqtt() -> AWSIoTMQTTClient:
    """Create and return a connected AWS IoT MQTT client."""
    client = AWSIoTMQTTClient(CLIENT_ID)
    client.configureEndpoint(AWS_ENDPOINT, 8883)
    client.configureCredentials(CA_PATH, KEY_PATH, CERT_PATH)

    # Connection resilience settings
    client.configureAutoReconnectBackoffTime(1, 32, 20)
    client.configureOfflinePublishQueueing(-1)   # unlimited queue
    client.configureDrainingFrequency(2)
    client.configureConnectDisconnectTimeout(10)
    client.configureMQTTOperationTimeout(5)

    client.connect()
    print(f"[MQTT] Connected to {AWS_ENDPOINT}")
    return client


def analyse(temp: float, hum: float, press: float) -> list[str]:
    """
    Run local analysis on the latest reading and return a list of insight strings.
    These are included in the MQTT payload so Lambda can store/act on them.
    """
    global previous_temp, temp_history
    insights = []

    # ── Spike detection ────────────────────────────────────────────────────────
    if previous_temp is not None:
        if abs(temp - previous_temp) > 5:
            insights.append("⚠ Temperature spike detected")

    # ── Trend detection (last 5 readings) ─────────────────────────────────────
    temp_history.append(temp)
    if len(temp_history) > 5:
        temp_history.pop(0)          # keep only the last 5

    if len(temp_history) == 5:
        if all(x < y for x, y in zip(temp_history, temp_history[1:])):
            insights.append("📈 Sustained temperature increase")
        elif all(x > y for x, y in zip(temp_history, temp_history[1:])):
            insights.append("📉 Sustained temperature decrease")

    # ── Predictive warning ─────────────────────────────────────────────────────
    if len(temp_history) >= 2:
        rate = temp_history[-1] - temp_history[-2]
        if rate > 0:
            predicted = temp + rate * 3
            # 40 °C is a sensible default high; Lambda checks against user threshold
            if predicted > 40:
                insights.append("⚠ Temperature likely to exceed threshold soon")

    previous_temp = temp
    return insights


def run():
    sense  = SenseHat()
    sense.clear()
    client = connect_mqtt()

    print("[Sensor] Starting data collection loop …")

    while True:
        try:
            temp  = round(sense.get_temperature(), 1)
            hum   = round(sense.get_humidity(),    1)
            press = round(sense.get_pressure(),     1)

            insights = analyse(temp, hum, press)

            payload = {
                "temperature": temp,
                "humidity":    hum,
                "pressure":    press,
                "analysis":    insights,
            }

            client.publish(TOPIC, json.dumps(payload), QoS=1)
            print(f"[MQTT] Published → {payload}")

            # Scroll summary on the LED matrix
            sense.show_message(
                f"T:{temp}C H:{hum}% P:{press}hPa",
                scroll_speed=0.075,
            )

            # Flash the matrix red if any warnings were detected
            if insights:
                sense.clear((255, 0, 0))
                time.sleep(0.5)
                sense.clear()

        except Exception as exc:
            print(f"[ERROR] {exc}")

        time.sleep(PUBLISH_INTERVAL)


if __name__ == "__main__":
    run()
