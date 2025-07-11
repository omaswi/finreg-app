import os
import psycopg2
from flask import Flask, jsonify, request
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

# Configure CORS to be more robust for API requests
CORS(app, resources={r"/api/*": {"origins": "*"}})

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

@app.route("/api/documents/<int:service_id>", methods=['GET'])
def get_documents_by_service(service_id):
    """
    API endpoint to fetch all documents for a specific financial service.
    """
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # SQL query to get documents based on the service_id
        sql_query = """
            SELECT d.documentID, d.title, dt.typeName, r.name as regulatorName
            FROM documents d
            JOIN document_types dt ON d.typeID = dt.typeID
            JOIN regulators r ON d.regulatorID = r.regulatorID
            JOIN document_services ds ON d.documentID = ds.documentID
            WHERE ds.serviceID = %s
            ORDER BY dt.typeName, d.title;
        """
        
        cur.execute(sql_query, (service_id,))
        
        docs_data = cur.fetchall()
        cur.close()
        
        docs_list = []
        for row in docs_data:
            docs_list.append({
                "documentID": row[0],
                "title": row[1],
                "typeName": row[2],
                "regulatorName": row[3]
            })
            
        return jsonify(docs_list)

@app.route("/api/login", methods=['POST'])
def admin_login():
    """
    API endpoint for administrator login.
    In a real app, this would involve password hashing and returning a JWT token.
    For this prototype, we'll do a simple check.
    """
    data = request.get_json()
    email = data.get('email')
    #password = data.get('password') # We won't check the password in this simple version

    if not email:
        return jsonify({"error": "Email is required."}), 400

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Check if a user exists with this email and the 'Administrator' role
        sql_query = """
            SELECT u.email FROM users u
            JOIN roles r ON u.roleID = r.roleID
            WHERE u.email = %s AND r.roleName = 'Administrator';
        """
        cur.execute(sql_query, (email,))
        
        admin_user = cur.fetchone()
        cur.close()
        
        if admin_user:
            # Successful login
            return jsonify({"success": True, "message": "Login successful."})
        else:
            # Failed login
            return jsonify({"error": "Invalid credentials or not an administrator."}), 401

    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()

    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()
