# app.py - with new routes for uploading

import os
import psycopg2
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.utils import secure_filename # New import for file handling
import PyPDF2 # New import for reading PDFs
from transformers import pipeline # New import for AI model

# --- AI Summarization Model ---
# This loads the model. In a real app, this would be done once on startup.
# For the hackathon, loading it here is fine.
summarizer = pipeline("summarization", model="sshleifer/distilbart-cnn-12-6")

# --- DATABASE CONNECTION CONFIGURATION ---
DB_CONFIG = {
    "dbname": os.environ.get('ATAS_DB_NAME', 'finreg'),
    "user": os.environ.get('ATAS_DB_USER'),
    "password": os.environ.get('ATAS_DB_PASS'),
    "host": os.environ.get('ATAS_DB_HOST'),
    "port": os.environ.get('ATAS_DB_PORT')
}
UPLOAD_FOLDER = '/app/uploads' # Directory to store uploaded files
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx'}

# --- FLASK APP INITIALIZATION ---
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
CORS(app, resources={r"/api/*": {"origins": "*"}})

# --- HELPER FUNCTION ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_pdf(file_stream):
    """Helper function to extract text from an uploaded PDF file."""
    try:
        pdf_reader = PyPDF2.PdfReader(file_stream)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()
        return text
    except Exception as e:
        print(f"Error extracting text from PDF: {e}")
        return ""

def summarize_text(text, max_chunk_size=1024):
    """Helper function to summarize long text using the AI model."""
    if not text:
        return ""
    try:
        # The model works best on chunks of text. We'll summarize the first chunk.
        summary = summarizer(text[:max_chunk_size], max_length=150, min_length=40, do_sample=False)
        return summary[0]['summary_text']
    except Exception as e:
        print(f"Error summarizing text: {e}")
        return "Summary could not be generated."

# --- API ENDPOINTS (EXISTING) ---
@app.route("/", methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "FinReg Portal API is running."})
	
@app.route("/api/login", methods=['POST'])
def admin_login():
    data = request.get_json()
    email = data.get('email')
    if not email:
        return jsonify({"error": "Email is required."}), 400
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        sql_query = "SELECT u.email FROM users u JOIN roles r ON u.roleID = r.roleID WHERE u.email = %s AND r.roleName = 'Administrator';"
        cur.execute(sql_query, (email,))
        admin_user = cur.fetchone()
        cur.close()
        if admin_user:
            return jsonify({"success": True, "message": "Login successful."})
        else:
            return jsonify({"error": "Invalid credentials or not an administrator."}), 401
    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()
			
@app.route("/api/financial-services", methods=['GET'])
def get_financial_services():
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT serviceID, serviceName, description FROM financial_services ORDER BY serviceName;")
        services_data = cur.fetchall()
        cur.close()
        services_list = []
        for row in services_data:
            services_list.append({"serviceID": row[0], "serviceName": row[1], "description": row[2]})
        return jsonify(services_list)
    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database connection failed: {error}"}), 502
    finally:
        if conn is not None:
            conn.close()
			
@app.route("/api/documents/<int:service_id>", methods=['GET'])
def get_documents_by_service(service_id):
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
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
            docs_list.append({"documentID": row[0], "title": row[1], "typeName": row[2], "regulatorName": row[3], "summary": row[4]})
        return jsonify(docs_list)
    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()

# --- ADMIN ENDPOINTS ---

@app.route("/api/regulators", methods=['GET'])
def get_regulators():
    """Endpoint to get all regulators for form dropdowns."""
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT regulatorID, name FROM regulators ORDER BY name;")
        data = cur.fetchall()
        cur.close()
        data_list = [{"regulatorID": row[0], "name": row[1]} for row in data]
        return jsonify(data_list)
    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route("/api/document-types", methods=['GET'])
def get_document_types():
    """Endpoint to get all document types for form dropdowns."""
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT typeID, typeName FROM document_types ORDER BY typeName;")
        data = cur.fetchall()
        cur.close()
        data_list = [{"typeID": row[0], "typeName": row[1]} for row in data]
        return jsonify(data_list)
    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route("/api/documents", methods=['POST'])
def create_document():
    """Admin endpoint to upload a new document."""
    # Check if the post request has the file part
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "File type not allowed"}), 400

    # Get metadata from the form
    title = request.form.get('title')
    regulator_id = request.form.get('regulatorID')
    type_id = request.form.get('typeID')
    service_ids = request.form.getlist('serviceIDs[]') # Get list of associated services
    uploader_id = 1 # In a real app, get this from the logged-in user's token

    # --- NEW AI LOGIC ---
    text_content = ""
    if allowed_file(file.filename):
        if file.filename.lower().endswith('.pdf'):
            text_content = extract_text_from_pdf(file)
    
    ai_summary = summarize_text(text_content)
    # --- END OF NEW AI LOGIC ---
	
    conn = None
    try:
        # Save the file
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)

        # Save metadata to database
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        # Insert into documents table and get the new ID
        sql_doc = "INSERT INTO documents (title, regulatorID, typeID, fileURL, uploadedBy, summary_AI) VALUES (%s, %s, %s, %s, %s, %s) RETURNING documentID;"
        cur.execute(sql_doc, (title, regulator_id, type_id, file_path, uploader_id, ai_summary))
        new_doc_id = cur.fetchone()[0]

        # Insert into the document_services junction table
        for service_id in service_ids:
            sql_junction = "INSERT INTO document_services (documentID, serviceID) VALUES (%s, %s);"
            cur.execute(sql_junction, (new_doc_id, service_id))

        conn.commit()
        cur.close()
        
        return jsonify({"success": True, "message": "File uploaded successfully.", "new_document": {"documentID": new_doc_id, "title": title}}), 201

    except (Exception, psycopg2.DatabaseError) as error:
        conn.rollback()
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route("/api/documents", methods=['GET'])
def get_all_documents():
    """Admin endpoint to get all documents."""
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT documentID, title FROM documents ORDER BY title;")
        all_docs_data = cur.fetchall()
        cur.close()
        docs_list = [{"documentID": row[0], "title": row[1]} for row in all_docs_data]
        return jsonify(docs_list)
    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()
            
@app.route("/api/documents/<int:document_id>", methods=['DELETE'])
def delete_document(document_id):
    """Admin endpoint to delete a document."""
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("DELETE FROM document_services WHERE documentID = %s;", (document_id,))
        cur.execute("DELETE FROM documents WHERE documentID = %s;", (document_id,))
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": f"Document {document_id} deleted."})
    except (Exception, psycopg2.DatabaseError) as error:
        conn.rollback()
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()
