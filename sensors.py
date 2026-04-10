from sense_emu import SenseHat
import sqlite3
import time

sense = SenseHat()
sense.clear()

# Connect to database
conn = sqlite3.connect("data.db")
c = conn.cursor()

#Analysis Variables
previous_temp = None
temp_history = []
latest_analysis = []


while True:
    try:
        #take readings from all 3 sensors
        temp = round(sense.get_temperature(), 1)
        press = round(sense.get_pressure(), 1)
        hum = round(sense.get_humidity(), 1)

        # add the readings to the database
        c.execute("""
        INSERT INTO readings (temperature, humidity, pressure)
        VALUES (?, ?, ?)
        """, (temp, hum, press))

        conn.commit()
    
        # create a message on sense HAT
        message = f"Temp: {temp}C Pressure: {press}hPa Humidity: {hum}%"

        # display message in terminal for debugging
        print(message)

        # display message
        sense.show_message(message, scroll_speed = 0.075)

        #Adjustable Warnings
        c.execute(" SELECT * from thresholds LIMIT 1")
        row = c.fetchone()

        if row:
            TEMP_MIN, TEMP_MAX, HUM_MIN, HUM_MAX, PRESS_MIN, PRESS_MAX = row
        
        # Warning system
        if temp < TEMP_MIN or temp > TEMP_MAX:
            sense.show_message("TEMP WARNING")
        elif hum < HUM_MIN or hum > HUM_MAX:
            sense.show_message("HUM WARNING")
        elif press < PRESS_MIN or press > PRESS_MAX:
            sense.show_message("PRESS WARNING")
        

        # Data Analysis

        #Spike Detection
        if previous_temp is not None:
            if abs(temp - previous_temp) > 5:
                latest_analysis.append("⚠ Spike Detected in temperature")
        

        #Trend Detection
        temp_history.append(temp)

        if len(temp_history) > 5:
            last = temp_history[-5:]

            if all (x < y for x, y in zip(last, last[1:])):
                latest_analysis.append("📈 Increasing temperature trend")

            elif all (x > y for x, y in zip( last, last[1:])):
                latest_analysis.append("📉 Decreasing temperature trend")
        

        #Prediction
        if len(temp_history) >= 2:
            rate = temp_history[-1] - temp_history[-2]

            if rate > 0:
                predicted = temp + rate * 3

                if predicted > TEMP_MAX:
                    latest_analysis.append("⚠ Temp likley to exceed threshold soon")
        
        #update previous
        previous_temp = temp
        latest_analysis = []

        time.sleep(2)

    except Exception as e:
        print("Error: ", e)
        time.sleep(2)

