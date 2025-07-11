import os
import psycopg2
from flask import Flask, jsonify
from flask_cors import CORS

# --- DATABASE CONNECTION CONFIGURATION ---
DB_CONFIG = {
    "dbname": os.environ.get('ATAS_DB_NAME', 'finreg'),
    "user": os.environ.get('ATAS_DB_USER'),
    "password": os.environ.get('ATAS_DB_PASS'),
    "host": os.environ.get('ATAS_DB_HOST'),
    "port": os.environ.get('ATAS_DB_PORT')
}

# --- FLASK APP INITIALIZATION ---
app = Flask(__name__)
CORS(app) # 2. Enable CORS for the entire app

# --- API ENDPOINTS ---
@app.route("/", methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "FinReg Portal API is running."})

@app.route("/api/financial-services", methods=['GET'])
def get_financial_services():
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT serviceID, serviceName, description FROM Financial_Services ORDER BY serviceName;")
        services_data = cur.fetchall()
        cur.close()
        
        services_list = []
        for row in services_data:
            services_list.append({
                "serviceID": row[0],
                "serviceName": row[1],
                "description": row[2]
            })
        return jsonify(services_list)
    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database connection failed: {error}"}), 502
    finally:
        if conn is not None:
            conn.close()
