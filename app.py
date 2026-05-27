"""
app.py
------
Flask web application for the Smart Environment Monitor.

All data is read from / written to AWS DynamoDB.
Authentication is handled via Amazon Cognito — the frontend exchanges
a Cognito username/password for a JWT which is sent as a Bearer token
on every protected API call.

Environment variables:
    AWS_REGION            – e.g. ap-southeast-2
    READINGS_TABLE        – DynamoDB table for sensor readings
    THRESHOLDS_TABLE      – DynamoDB table for thresholds
    COGNITO_USER_POOL_ID  – Cognito User Pool ID
    COGNITO_CLIENT_ID     – Cognito App Client ID
    DEVICE_ID             – logical device identifier
    SECRET_KEY            – Flask session secret

Run:
    python app.py
"""

import os
import json
import boto3
import jwt          # pip install PyJWT
import requests
from functools import wraps
from datetime import datetime, timezone
from decimal import Decimal
from flask import Flask, jsonify, render_template, request, abort
from boto3.dynamodb.conditions import Key

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

# ── AWS clients ────────────────────────────────────────────────────────────────
AWS_REGION       = os.environ.get("AWS_REGION", "ap-southeast-2")
READINGS_TABLE   = os.environ.get("READINGS_TABLE",   "sem_readings")
THRESHOLDS_TABLE = os.environ.get("THRESHOLDS_TABLE", "sem_thresholds")
DEVICE_ID        = os.environ.get("DEVICE_ID",        "smart-env-monitor")

COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID    = os.environ.get("COGNITO_CLIENT_ID",    "")
COGNITO_REGION       = AWS_REGION

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
cognito  = boto3.client("cognito-idp", region_name=AWS_REGION)

# Cache Cognito JWKS so we don't fetch on every request
_jwks_cache = None


# ── Auth helpers ───────────────────────────────────────────────────────────────

def get_cognito_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is None:
        url = (
            f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
            f"{COGNITO_USER_POOL_ID}/.well-known/jwks.json"
        )
        _jwks_cache = requests.get(url, timeout=5).json()
    return _jwks_cache


def verify_token(token: str) -> dict:
    """
    Verify a Cognito JWT and return its claims.
    Raises jwt.exceptions.* on failure.
    """
    jwks    = get_cognito_jwks()
    header  = jwt.get_unverified_header(token)
    key_obj = next(
        k for k in jwks["keys"] if k["kid"] == header["kid"]
    )
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_obj))

    return jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience=COGNITO_CLIENT_ID,
    )


def require_auth(f):
    """Decorator: reject requests without a valid Cognito JWT."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            abort(401)
        token = auth_header.split(" ", 1)[1]
        try:
            request.user_claims = verify_token(token)
        except Exception:
            abort(401)
        return f(*args, **kwargs)
    return wrapper


# ── DynamoDB helpers ───────────────────────────────────────────────────────────

def _float(val):
    """DynamoDB returns Decimal; convert to float for JSON serialisation."""
    return float(val) if isinstance(val, Decimal) else val


def get_readings(limit: int = 50) -> list[dict]:
    table = dynamodb.Table(READINGS_TABLE)
    resp  = table.query(
        KeyConditionExpression=Key("device_id").eq(DEVICE_ID),
        ScanIndexForward=False,   # newest first
        Limit=limit,
    )
    return [
        {
            "temperature": _float(r["temperature"]),
            "humidity":    _float(r["humidity"]),
            "pressure":    _float(r["pressure"]),
            "timestamp":   r["timestamp"],
            "analysis":    r.get("analysis", []),
            "alerts":      r.get("alerts",   []),
        }
        for r in resp.get("Items", [])
    ]


def get_latest_reading() -> dict | None:
    rows = get_readings(limit=1)
    return rows[0] if rows else None


def get_thresholds() -> dict:
    table = dynamodb.Table(THRESHOLDS_TABLE)
    resp  = table.get_item(Key={"device_id": DEVICE_ID})
    item  = resp.get("Item")
    if item:
        return {k: _float(v) for k, v in item.items() if k != "device_id"}
    return {
        "temp_min": 0,   "temp_max": 40,
        "hum_min":  10,  "hum_max": 90,
        "press_min": 970, "press_max": 1030,
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("index.html",
                           cognito_client_id=COGNITO_CLIENT_ID,
                           cognito_region=COGNITO_REGION,
                           cognito_user_pool_id=COGNITO_USER_POOL_ID)


@app.route("/auth/login", methods=["POST"])
def login():
    """Exchange username/password for Cognito tokens."""
    body = request.json or {}
    username = body.get("username", "")
    password = body.get("password", "")

    if not username or not password:
        return jsonify({"error": "username and password required"}), 400

    try:
        resp = cognito.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
            ClientId=COGNITO_CLIENT_ID,
        )
        tokens = resp["AuthenticationResult"]
        return jsonify({
            "accessToken":  tokens["AccessToken"],
            "idToken":      tokens["IdToken"],
            "refreshToken": tokens["RefreshToken"],
            "expiresIn":    tokens["ExpiresIn"],
        })
    except cognito.exceptions.NotAuthorizedException:
        return jsonify({"error": "Invalid credentials"}), 401
    except cognito.exceptions.UserNotFoundException:
        return jsonify({"error": "User not found"}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/auth/refresh", methods=["POST"])
def refresh():
    """Exchange a refresh token for new access/id tokens."""
    body = request.json or {}
    refresh_token = body.get("refreshToken", "")
    if not refresh_token:
        return jsonify({"error": "refreshToken required"}), 400

    try:
        resp = cognito.initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
            ClientId=COGNITO_CLIENT_ID,
        )
        tokens = resp["AuthenticationResult"]
        return jsonify({
            "accessToken": tokens["AccessToken"],
            "idToken":     tokens["IdToken"],
            "expiresIn":   tokens["ExpiresIn"],
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 401


@app.route("/current")
@require_auth
def current():
    reading = get_latest_reading()
    if reading:
        return jsonify(reading)
    return jsonify({"error": "no data available"}), 404


@app.route("/history")
@require_auth
def history():
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify(get_readings(limit=limit))


@app.route("/thresholds", methods=["GET"])
@require_auth
def thresholds_get():
    return jsonify(get_thresholds())


@app.route("/update-thresholds", methods=["POST"])
@require_auth
def update_thresholds():
    data  = request.json or {}
    table = dynamodb.Table(THRESHOLDS_TABLE)

    required = ["tempMin", "tempMax", "humMin", "humMax", "pressMin", "pressMax"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing threshold fields"}), 400

    table.put_item(Item={
        "device_id": DEVICE_ID,
        "temp_min":  Decimal(str(data["tempMin"])),
        "temp_max":  Decimal(str(data["tempMax"])),
        "hum_min":   Decimal(str(data["humMin"])),
        "hum_max":   Decimal(str(data["humMax"])),
        "press_min": Decimal(str(data["pressMin"])),
        "press_max": Decimal(str(data["pressMax"])),
    })
    return jsonify({"status": "updated"})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
