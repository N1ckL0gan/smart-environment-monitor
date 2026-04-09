from flask import Flask, jsonify, render_template
import sqlite3

app = Flask(__name__)

@app.route("/")
def home():
    return render_template("index.html")

def get_db():
    return sqlite3.connect("data.db")


@app.route("/current")
def current():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM readings ORDER BY timestamp DESC LIMIT 1") # select the most recent data
    row = c.fetchone()

    conn.close()

    if row:
        return jsonify({
            "temperature": row[1],
            "humidity": row[2],
            "pressure": row[3],
            "timestamp": row[4]
        })
    return jsonify({"error": "no data available"})

@app.route("/history")
def history():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM readings ORDER BY timestamp DESC LIMIT 20") # select last 20 data inputs
    rows = c.fetchall()

    conn.close()

    data = []
    for r in rows:
        data.append({
            "temperature": r[1],
            "humidity": r[2],
            "pressure": r[3],
            "timestamp": r[4]
        })
    return jsonify(data)

app.run(host="0.0.0.0", port = 5000)
