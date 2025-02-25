from flask import Flask # Web framework for creating API's
from flask import request # Handles incoming data (eg.JSON requests)
from flask import jsonify # Converts python dictionaries into JSON responses
from flask import redirect # Sends the user to another URL when they visit a route 
import sqlite3 # Connects to a SQLite database to store and redirect to urls
import uuid # Generates unique short codes
import re # Validates if the input is a proper URL
from datetime import datetime, timedelta # handles expiry dates for URLs

app = Flask(__name__) # creates a flask app instance allowing us to define API routes

conn = sqlite3.connect("urls.db", check_same_thread=False)  # Creates a connection to SQLite database
cursor = conn.cursor()  # Pointer to fetch data

# Create the table if it doesn't exist with 4 colums (id,org_url,created_at,expires_at)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS urls (
        id TEXT PRIMARY KEY,
        org_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP
    )
""")

# Ensure 'expires_at' column exists (for older databases) as i didnt include expiry feature in my primary code
cursor.execute("PRAGMA table_info(urls)")  # Get table structure
columns = [col[1] for col in cursor.fetchall()]  # Extract column names 

if "expires_at" not in columns:  # If "expires_at" column doesn't exist, add it
    cursor.execute("ALTER TABLE urls ADD COLUMN expires_at TIMESTAMP") # Ensures that older version of the database has the expires_at column
    conn.commit() # Make the changes permanent

conn.commit() # Make the changes permanent


def generate_short_url():
    return str(uuid.uuid4())[:6] # create a universal unique identifier


def is_valid_url(org_url):
    regex = re.compile(
        r'^(https?://)?'  # Optional http or https
        r'(([A-Za-z0-9-]+\.)+[A-Za-z]{2,6})'  # Domain name
        r'(:\d+)?(/.*)?$',  # Optional port and path
        re.IGNORECASE
    )
    return re.match(regex, org_url) is not None # checks if the url starts with specified pattern

@app.route('/shorten',methods=['POST']) # Flask decorator which accepts post request to shorten url

def shorten_url():
    conn = sqlite3.connect("urls.db")  # Use a new connection
    cursor = conn.cursor() # New pointer to fetch data ( to prevent issues like locked transactions or concurrency problems )
    data = request.get_json()  # Get json input from the user
    org_url = data.get("org_url") # Extract original url from the json
    expiry_days = data.get("expiry_days", 7) # Get expiry days if exist or set it to 7 (default)
    if not org_url or not is_valid_url(org_url):
        return jsonify({"error":"URL is required"}), 400 # Validate if a valid url is provided 
    try:
        expiry_days = int(expiry_days) # Convert expiry days to integer
        print(f"DEBUG: Converted expiry_days = {expiry_days}") 
        if expiry_days < 1 or expiry_days > 365:  # Ensure Expiry day is between 1 and 365
            return jsonify({"error": "Expiry time must be between 1 and 365 days"}), 400 
    except ValueError:
        return jsonify({"error": "Invalid expiry_days value"}), 400 # Handle cases where expiry days is not valid number
    cursor.execute("SELECT id, expires_at FROM urls WHERE org_url=?", (org_url,)) 
    existing_entry = cursor.fetchone() # Check if url already exist in the database
    if(existing_entry):
        short_url=existing_entry[0] # Existing short url
        expiry_date=existing_entry[1] # Existing expiry date if exist
        if not expiry_date:
            expiry_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S.%f") # If no expiry date it sets expiry date from now (I used this as the previous version dont have expiry date feature. For new database this is not required)
            cursor.execute("UPDATE urls SET expires_at=? WHERE id=?", (expiry_date, short_url)) # Setting expiry date
            conn.commit()
    else:
        short_url = generate_short_url() # Generates a short url
        expiry_date = (datetime.now() + timedelta(days=expiry_days)).strftime("%Y-%m-%d %H:%M:%S.%f") # Compute expiry date
        cursor.execute("INSERT INTO urls (id, org_url, expires_at) VALUES (?, ?, ?)",(short_url, org_url, expiry_date)) # Insert new url with expiry date and short url 
        conn.commit()
    return jsonify({"short_url": f"http://localhost:5000/{short_url}", "expires_at": expiry_date}) # Return short url and expiry date

@app.route('/<short_id>',methods=['GET']) # Handles get request for shortened url

def get_original_url(short_id):
    conn = sqlite3.connect("urls.db")
    cursor = conn.cursor()

    cursor.execute("SELECT org_url, expires_at FROM urls WHERE id=?", (short_id,)) # Getting the original url for shortened one
    result = cursor.fetchone()

    if result:
        original_url = result[0] # Extracts the original url
        expires_at = result[1]  # Extracts expiry date (could be Null when using the same databse without expiry date handling feature)

        # If expires_at is None, treat it as a non-expiring link
        if expires_at:
            expires_at = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S.%f")  # Converts string to Datetime
            if expires_at < datetime.now():
                # If expired, delete it and return "410 Gone"
                cursor.execute("DELETE FROM urls WHERE id=?", (short_id,))
                conn.commit()
                return jsonify({"error": "Short URL expired"}), 410  

        return redirect(original_url)  # Redirect if still valid
    else:
        return jsonify({"error": "Short URL not found"}), 404 # return 404 if the url is not found

if __name__ == '__main__':
    app.run(debug=True) # Starts Flask web application with debugging enabled