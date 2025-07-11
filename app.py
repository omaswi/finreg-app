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
            SELECT d.documentID, d.title, dt.typeName, r.name as regulatorName, d.summary_ai
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
    # --- SIMULATE LOGGED-IN ADMIN ---
    # In a real app, you'd get the userID from a secure session or JWT token.
    # For this prototype, we'll assume the admin with userID=1 is logged in.
    uploader_id = 1

    # 1. Validate the incoming request and file
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "File type not allowed"}), 400

    # 2. Extract metadata from the form
    title = request.form.get('title')
    type_id = request.form.get('typeID')
    service_ids = request.form.getlist('serviceIDs[]')

    # 3. Process the file to get text and AI summary
    text_content = ""
    if file.filename.lower().endswith('.pdf'):
        text_content = extract_text_from_pdf(file)
    ai_summary = summarize_text(text_content)
    
    # Rewind the file stream before saving
    file.seek(0)
    
    # 4. Save the file
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)

    # 5. Save everything to the database
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Get the admin's associated regulator ID
        cur.execute("SELECT regulatorid FROM users WHERE userid = %s;", (uploader_id,))
        result = cur.fetchone()
        if not result or result[0] is None:
            return jsonify({"error": "Admin user is not associated with a regulator."}), 403
        admin_regulator_id = result[0]

        # Insert document metadata into the 'documents' table
        sql_doc = "INSERT INTO documents (title, regulatorid, typeid, fileurl, uploadedby, summary_ai) VALUES (%s, %s, %s, %s, %s, %s) RETURNING documentid;"
        cur.execute(sql_doc, (title, admin_regulator_id, type_id, file_path, uploader_id, ai_summary))
        new_doc_id = cur.fetchone()[0]

        # Link the new document to the selected financial services
        for service_id in service_ids:
            sql_junction = "INSERT INTO document_services (documentid, serviceid) VALUES (%s, %s);"
            cur.execute(sql_junction, (new_doc_id, service_id))

        conn.commit()
        cur.close()
        
        return jsonify({"success": True, "message": "File uploaded successfully.", "new_document": {"documentID": new_doc_id, "title": title}}), 201

    except (Exception, psycopg2.DatabaseError) as error:
        conn.rollback()
        return jsonify({"error": f"Database error: {str(error)}"}), 500
    finally:
        if conn is not None:
            conn.close()
        
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

@app.route("/api/chatbot", methods=['POST'])
def chatbot_query():
    """
    Receives a user query and finds the best matching FAQ.
    Uses a simple keyword scoring algorithm.
    """
    data = request.get_json()
    user_query = data.get('query', '').lower()
    
    if not user_query:
        return jsonify({"answer": "Please ask a question."})

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT question, answer FROM faqs;")
        all_faqs = cur.fetchall()
        cur.close()

        best_match_answer = "I'm sorry, I don't have an answer for that. Please try rephrasing your question or contact a regulatory authority directly."
        highest_score = 0
        query_words = set(user_query.split())

        for question, answer in all_faqs:
            question_words = set(question.lower().split())
            # Calculate score based on number of matching words
            score = len(query_words.intersection(question_words))

            if score > highest_score:
                highest_score = score
                best_match_answer = answer
        
        # We'll consider a score of 2 or more a decent match
        if highest_score < 2:
             return jsonify({"answer": "I'm sorry, I don't have a specific answer for that. You can browse the documents or contact a regulator for more help."})


        return jsonify({"answer": best_match_answer})

    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"answer": f"An error occurred: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()
		
@app.route("/api/faqs", methods=['GET'])
def get_all_faqs():
    """Admin endpoint to get all FAQs."""
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT faqID, question, answer FROM faqs ORDER BY question;")
        all_faqs_data = cur.fetchall()
        cur.close()
        faqs_list = [{"faqID": row[0], "question": row[1], "answer": row[2]} for row in all_faqs_data]
        return jsonify(faqs_list)
    except (Exception, psycopg2.DatabaseError) as error:
        return jsonify({"error": f"Database error: {error}"}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route("/api/faqs", methods=['POST'])
def create_faq():
    """Admin endpoint to create a new FAQ."""
    data = request.get_json()
    question = data.get('question')
    answer = data.get('answer')
    if not question or not answer:
        return jsonify({"error": "Question and answer are required."}), 400
    
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("INSERT INTO faqs (question, answer) VALUES (%s, %s) RETURNING faqID;", (question, answer))
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return jsonify({"success": True, "new_faq": {"faqID": new_id, "question": question, "answer": answer}}), 201
    except (Exception, psycopg2.DatabaseError) as error:
        conn.rollback()
        return jsonify({"error": str(error)}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route("/api/faqs/<int:faq_id>", methods=['DELETE'])
def delete_faq(faq_id):
    """Admin endpoint to delete an FAQ."""
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("DELETE FROM faqs WHERE faqID = %s;", (faq_id,))
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": f"FAQ {faq_id} deleted."})
    except (Exception, psycopg2.DatabaseError) as error:
        conn.rollback()
        return jsonify({"error": str(error)}), 500
    finally:
        if conn is not None:
            conn.close()
