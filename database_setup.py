import sqlite3

conn = sqlite3.connect("data.db")
c = conn.cursor()

# Readings Table
c.execute("""
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temperature REAL,
    humidity REAL,
    pressure REAL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")

#Thresholds Table
c.execute("""
CREATE TABLE IF NOT EXISTS thresholds (
        temp_min REAL,
        temp_max REAL,
        hum_min REAL,
        hum_max REAL,
        press_min REAL,
        press_max REAL
          )
          """)

# Insert default thresholds if table is empty
c.execute("SELECT COUNT (*) FROM thresholds")
count = c.fetchone()[0]

if count == 0:
    c.execute("""
    INSERT INTO thresholds VALUES (0, 40, 10, 90, 970, 1030)
    """)


conn.commit()
conn.close()

print("Database Ready")