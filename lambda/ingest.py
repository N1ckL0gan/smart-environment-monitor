"""
lambda/ingest.py
----------------
AWS Lambda function triggered by an IoT Core Rule when a message arrives
on the  smartenv/readings  MQTT topic.

The function:
  1. Stores the raw reading in DynamoDB (readings table).
  2. Fetches the current thresholds for the device.
  3. Checks thresholds and analysis flags.
  4. Publishes an SNS notification if any alert condition is met.
  5. Logs metrics to CloudWatch via boto3.

Environment variables (set in Lambda console or Terraform):
  READINGS_TABLE   – DynamoDB table name for sensor readings
  THRESHOLDS_TABLE – DynamoDB table name for per-user thresholds
  SNS_TOPIC_ARN    – ARN of the SNS topic for alerts
  DEVICE_ID        – Logical device identifier (default: "smart-env-monitor")
"""

import os
import json
import boto3
import logging
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb  = boto3.resource("dynamodb")
sns       = boto3.client("sns")
cloudwatch = boto3.client("cloudwatch")

READINGS_TABLE   = os.environ["READINGS_TABLE"]
THRESHOLDS_TABLE = os.environ["THRESHOLDS_TABLE"]
SNS_TOPIC_ARN    = os.environ["SNS_TOPIC_ARN"]
DEVICE_ID        = os.environ.get("DEVICE_ID", "smart-env-monitor")


# ── Helpers ────────────────────────────────────────────────────────────────────

def to_decimal(value):
    """Convert float to Decimal for DynamoDB compatibility."""
    return Decimal(str(value))


def get_thresholds() -> dict:
    """
    Fetch threshold row from DynamoDB. Returns defaults if not yet configured.
    """
    table = dynamodb.Table(THRESHOLDS_TABLE)
    resp  = table.get_item(Key={"device_id": DEVICE_ID})
    item  = resp.get("Item")

    if item:
        return {k: float(v) for k, v in item.items() if k != "device_id"}

    # Sensible defaults
    return {
        "temp_min":  0,   "temp_max":  40,
        "hum_min":  10,   "hum_max":  90,
        "press_min": 970, "press_max": 1030,
    }


def store_reading(payload: dict, timestamp: str, alerts: list[str]) -> None:
    """Write a single reading to the DynamoDB readings table."""
    table = dynamodb.Table(READINGS_TABLE)
    table.put_item(Item={
        "device_id":   DEVICE_ID,
        "timestamp":   timestamp,
        "temperature": to_decimal(payload["temperature"]),
        "humidity":    to_decimal(payload["humidity"]),
        "pressure":    to_decimal(payload["pressure"]),
        "analysis":    payload.get("analysis", []),
        "alerts":      alerts,
    })


def publish_alerts(alerts: list[str]) -> None:
    """Send an SNS notification if there are active alerts."""
    if not alerts:
        return

    message = (
        "⚠ Smart Environment Monitor – Alert\n\n"
        + "\n".join(f"• {a}" for a in alerts)
        + "\n\nCheck your dashboard for details."
    )

    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject="Smart Environment Alert",
        Message=message,
    )
    logger.info(f"SNS alert sent: {alerts}")


def push_cloudwatch_metrics(temp: float, hum: float, press: float) -> None:
    """Emit custom metrics to CloudWatch for analytics and dashboards."""
    namespace  = "SmartEnvironmentMonitor"
    dimensions = [{"Name": "DeviceId", "Value": DEVICE_ID}]

    cloudwatch.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {"MetricName": "Temperature", "Dimensions": dimensions,
             "Value": temp,  "Unit": "None"},
            {"MetricName": "Humidity",    "Dimensions": dimensions,
             "Value": hum,   "Unit": "Percent"},
            {"MetricName": "Pressure",    "Dimensions": dimensions,
             "Value": press, "Unit": "None"},
        ],
    )


# ── Handler ────────────────────────────────────────────────────────────────────

def handler(event, context):
    """
    Lambda entry point.
    The IoT Rule passes the MQTT JSON payload directly as `event`.
    """
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        temp  = float(event["temperature"])
        hum   = float(event["humidity"])
        press = float(event["pressure"])
        analysis = event.get("analysis", [])
    except (KeyError, ValueError) as exc:
        logger.error(f"Malformed payload: {exc}")
        return {"statusCode": 400, "body": "Malformed payload"}

    timestamp  = datetime.now(timezone.utc).isoformat()
    thresholds = get_thresholds()
    alerts     = list(analysis)   # start with device-side insights

    # ── Threshold checks ───────────────────────────────────────────────────────
    if temp  < thresholds["temp_min"]  or temp  > thresholds["temp_max"]:
        alerts.append(f"⚠ Temperature {temp}°C is out of range "
                      f"({thresholds['temp_min']}–{thresholds['temp_max']}°C)")

    if hum   < thresholds["hum_min"]   or hum   > thresholds["hum_max"]:
        alerts.append(f"⚠ Humidity {hum}% is out of range "
                      f"({thresholds['hum_min']}–{thresholds['hum_max']}%)")

    if press < thresholds["press_min"] or press > thresholds["press_max"]:
        alerts.append(f"⚠ Pressure {press} hPa is out of range "
                      f"({thresholds['press_min']}–{thresholds['press_max']} hPa)")

    # ── Persist & notify ───────────────────────────────────────────────────────
    store_reading(event, timestamp, alerts)
    push_cloudwatch_metrics(temp, hum, press)
    publish_alerts(alerts)

    logger.info(f"Processed reading at {timestamp} — alerts: {alerts}")
    return {"statusCode": 200, "body": "OK"}
