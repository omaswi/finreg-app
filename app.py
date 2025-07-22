import os
import psycopg2
import json
import secrets
from datetime import timedelta
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, session, g
from flask_cors import CORS
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import PyPDF2
from PyPDF2 import PdfReader
import time
from openai import OpenAI
from io import BytesIO
client = OpenAI()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# --- DATABASE CONNECTION CONFIGURATION ---
DB_CONFIG = {
    "dbname": os.environ.get('ATAS_DB_NAME', 'finreg'),
    "user": os.environ.get('ATAS_DB_USER'),
    "password": os.environ.get('ATAS_DB_PASS'),
    "host": os.environ.get('ATAS_DB_HOST'),
    "port": os.environ.get('ATAS_DB_PORT')
}
UPLOAD_FOLDER = '/app/uploads'
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx'}

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8)
)
CORS(app, supports_credentials=True, origins=["http://127.0.0.1:8080", "http://127.0.0.1:8001", "null"])
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

class AuditLogger:
    def __init__(self, db_config):
        self.db_config = db_config
    
    def log(self, user_id, action, target_id=None, metadata=None):
        conn = None
        try:
            conn = psycopg2.connect(**self.db_config)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_trail (userID, action, targetID, additional_info)
                    VALUES (%s, %s, %s, %s::jsonb);
                """, (user_id, action, target_id, json.dumps(metadata) if metadata else None))
                conn.commit()
        except psycopg2.Error as e:
            app.logger.error(f"Audit log failed: {str(e)}")
            if conn: conn.rollback()
        finally:
            if conn: conn.close()

audit_logger = AuditLogger(DB_CONFIG)

@app.before_request
def load_user_id_to_g():
    g.user_id = session.get('user_id')
    print(f"BEFORE_REQUEST: g.user_id = {g.user_id}, session.get('user_id') = {session.get('user_id')}")

def audit_action(action_name, user_id_getter=None, target_id_param=None):

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # Get the target ID if specified
            target_id = kwargs.get(target_id_param) if target_id_param else None
            print(f"AUDIT_ACTION Decorator: Executing for endpoint {request.endpoint}")
            print(f"AUDIT_ACTION Decorator: Current session = {session}")
            print(f"AUDIT_ACTION Decorator: kwargs for target_id_param '{target_id_param}' = {kwargs}")
            print(f"AUDIT_ACTION Decorator: Determined target_id = {target_id}")
            
            user_id = None
            if user_id_getter:
                user_id = user_id_getter(request)
                print("USER ID FROM ID GETTER",user_id)
            if not user_id and hasattr(g, 'user_id'):
                user_id = g.user_id
            if not user_id and 'user_id' in session:
                user_id = session['user_id'] 
            else:
                user_id = session.get('user_id')
            print(f"AUDIT_ACTION Decorator: Determined user_id for logging = {user_id}")
            # Execute the endpoint
            response = f(*args, **kwargs)
            
            # Prepare metadata
            metadata = {
                'endpoint': request.endpoint,
                'method': request.method,
                'status_code': response.status_code,
                'path': request.path,
                'ip': request.remote_addr,
                'user_agent': request.user_agent.string
            }
            
            # Log the action
            audit_logger.log(
                user_id=user_id,
                action=action_name,
                target_id=kwargs.get(target_id_param) if target_id_param else None,
                metadata=metadata
            )
            print(f"AUDIT_ACTION Decorator: Logged action '{action_name}' for user {user_id} on target {target_id}")
            return response
        return wrapped
    return decorator

def log_system_action(action, target_type=None, target_id=None, details=None):
    metadata = {
        'system_action': True,
        'target_type': target_type
    }
    if details:
        metadata.update(details)
    
    return audit_logger.log(
        user_id=None,  # Will be NULL in database
        action=action,
        target_id=target_id,
        metadata=metadata
    )


# === PUBLIC-FACING API ENDPOINTS ===

@app.before_request
def load_user_from_session():
    g.user_id = session.get('user_id')

# --- HELPER FUNCTIONS ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- HELPER FUNCTIONS ---
def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

def extract_text_from_pdf(file_stream):
    try:
        pdf_reader = PyPDF2.PdfReader(file_stream)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        print(f"Error extracting text from PDF: {e}")
        return ""

def chunk_text(text, words_per_chunk=2800):
    """Split text into manageable chunks"""
    words = text.split()
    return [' '.join(words[i:i + words_per_chunk]) 
           for i in range(0, len(words), words_per_chunk)]

def summarize_with_gpt(text_chunk):
    """Generate summary for a text chunk"""
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": """
                You are a professional advisor at Financial Regulations Center.
                 Create a concise summary of key points from documents to make it easy for your clients to understand."""},
                {"role": "user", "content": text_chunk[:8000]}
            ],
            max_tokens=150,
            temperature=0.3
        )
        return response.choices[0].message.content
    except openai.RateLimitError:
      #  time.sleep(60)
        return summarize_with_gpt(text_chunk)
    except Exception as e:
        app.logger.error(f"GPT error: {str(e)}")
        return None

def generate_ai_summary(file):
    """Main summarization function"""
    try:
        text = extract_text_from_pdf(file)
        if not text.strip():
            return "No extractable text found"
        
        chunks = chunk_text(text)[:5]  # Limit to 5 chunks to control costs
        if not chunks:
            return "Text too short for summary"
        
        summaries = []
        for i, chunk in enumerate(chunks):
            summary = summarize_with_gpt(chunk)
            if summary:
                summaries.append(summary)
           # if (i + 1) % 3 == 0:  # Rate limit handling
            #    time.sleep(60)
        
        if not summaries:
            return "Could not generate summary"
            
        # Combine summaries
        combined = "\n".join(summaries)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Combine these into a cohesive summary:"},
                {"role": "user", "content": combined}
            ],
            max_tokens=500,
            temperature=0.3
        )
        return response.choices[0].message.content
        
    except Exception as e:
        app.logger.error(f"Summary generation failed: {str(e)}")
        return "AI summary unavailable"

# === PUBLIC-FACING API ENDPOINTS ===

@app.route("/", methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "FinReg Portal API is running."})

@app.route("/api/financial-services", methods=['POST'])
@audit_action("financial_service_created", target_id_param=new_id)
def create_financial_service():
    data = request.get_json()
    serviceName = data.get('serviceName')
    description = data.get('description', '') # Description is optional

    if not serviceName:
        return jsonify({"error": "Service name is required."}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO financial_services (servicename, description) VALUES (%s, %s) RETURNING serviceid;", (serviceName, description))
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return jsonify({"success": True, "new_financial_service": {"serviceID": new_id, "serviceName": serviceName}}), 201
    except (Exception, psycopg2.DatabaseError) as error:
        if conn:
            conn.rollback()
        return jsonify({"error": str(error)}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route("/api/financial-services/<int:service_id>", methods=['PUT'])
@audit_action("financial_service_updated", target_id_param="service_id")
def update_financial_service(service_id):
    data = request.get_json()
    serviceName = data.get('serviceName')
    if not serviceName:
        return jsonify({"error": "serviceName is required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE financial_services SET servicename = %s WHERE serviceid = %s;", (serviceName, service_id))
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": "Financial service updated."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/financial-services/<int:service_id>", methods=['DELETE'])
def delete_financial_service(service_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM financial_services WHERE serviceid = %s;", (service_id,))
        conn.commit()
        cur.close()
        return jsonify({"success": True})
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Cannot delete: this service is linked to existing documents."}), 409
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/financial-services", methods=['GET'])
def get_financial_services():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT serviceid AS id, servicename AS name, description FROM financial_services ORDER BY servicename;")
        data = cur.fetchall()
        data_list = [{"id": row[0], "name": row[1], "description": row[2]} for row in data]
        return jsonify(data_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/documents/<int:service_id>", methods=['GET'])
def get_documents_by_service(service_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        sql = """
            SELECT d.documentid, d.title, dt.typename, r.name as regulatorname, d.summary_ai
            FROM documents d
            JOIN document_types dt ON d.typeid = dt.typeid
            JOIN regulators r ON d.regulatorid = r.regulatorid
            JOIN document_services ds ON d.documentid = ds.documentid
            WHERE ds.serviceid = %s ORDER BY dt.typename, d.title;
        """
        cur.execute(sql, (service_id,))
        data = cur.fetchall()
        data_list = [{"documentID": row[0], "title": row[1], "typeName": row[2], "regulatorName": row[3], "summary": row[4]} for row in data]
        return jsonify(data_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/download/<int:document_id>", methods=['GET'])
def download_document(document_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT fileurl FROM documents WHERE documentid = %s;", (document_id,))
        result = cur.fetchone()
        if result:
            file_path = result[0]
            directory = os.path.dirname(file_path)
            filename = os.path.basename(file_path)
            return send_from_directory(directory, filename, as_attachment=True)
        else:
            return jsonify({"error": "File not found."}), 404
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500
    finally:
        if conn: conn.close()

@app.route("/api/chatbot", methods=['POST'])
def chatbot_query():
    data = request.get_json()
    user_query = data.get('query', '').lower()
    if not user_query: return jsonify({"answer": "Please ask a question."})
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT question, answer FROM faqs;")
        all_faqs = cur.fetchall()
        best_match_answer = "I'm sorry, I don't have an answer for that."
        highest_score = 0
        query_words = set(user_query.split())
        for question, answer in all_faqs:
            question_words = set(question.lower().split())
            score = len(query_words.intersection(question_words))
            if score > highest_score:
                highest_score = score
                best_match_answer = answer
        if highest_score < 2:
            best_match_answer = "I'm sorry, I don't have a specific answer for that. Please try rephrasing."
        return jsonify({"answer": best_match_answer})
    except Exception as e:
        return jsonify({"answer": f"An error occurred: {str(e)}"}), 500
    finally:
        if conn: conn.close()

# === USER AUTHENTICATION & REGISTRATION ===

@app.route("/api/register", methods=['POST'])
def register_user():
    if 'profilePDF' not in request.files: return jsonify({"error": "Profile PDF is missing."}), 400
    file = request.files['profilePDF']
    email = request.form.get('email')
    password = request.form.get('password')
    user_type_id = request.form.get('userTypeID')
    if not all([email, password, user_type_id, file.filename]):
        return jsonify({"error": "All fields are required."}), 400
    if not allowed_file(file.filename): return jsonify({"error": "Invalid file type."}), 400
    
    filename = secure_filename(f"profile_{email}_{file.filename}")
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)
    password_hash = generate_password_hash(password)
    public_user_role_id = 3 # Assuming 'Public User' has roleID = 3

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        sql = "INSERT INTO users (email, passwordhash, roleid, usertypeid, profiledetails) VALUES (%s, %s, %s, %s, %s);"
        cur.execute(sql, (email, password_hash, public_user_role_id, user_type_id, file_path))
        conn.commit()
        return jsonify({"success": True, "message": "User registered successfully."}), 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "This email address is already registered."}), 409
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/login", methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    if not email or not password: return jsonify({"error": "Email and password are required."}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        sql = "SELECT u.passwordhash, r.rolename, u.userid FROM users u JOIN roles r ON u.roleid = r.roleid WHERE u.email = %s AND u.is_archived = FALSE;"
        cur.execute(sql, (email,))
        user_data = cur.fetchone()
        if user_data and check_password_hash(user_data[1], password):
            session.permanent = True
            session['user_id'] = user_data[0]
            session['user_role'] = user_data[2]
            g.user_id = user_data[0]
            audit_logger.log(user_id=g.user_id, action="user_login_success", metadata={"email": email})
            return jsonify({"success": True, "message": "Login successful.", "role": user_data[2], "userID": user_data[0]})
        else:
            audit_logger.log(user_id=None, action="user_login_failed", metadata={"email": email})
            return jsonify({"error": "Invalid email or password."}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/logout", methods=['POST'])
def logout():
    user_id = session.get('user_id')
    if user_id:
        audit_logger.log(user_id=user_id, action="user_logout")
    session.clear()
    return jsonify({"success": True, "message": "You have been logged out."})

# === ADMIN-ONLY CRUD ENDPOINTS ===

@app.route("/api/roles", methods=['GET'])
def get_roles():
    """Endpoint to fetch all available user roles."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT roleid, rolename FROM roles ORDER BY rolename;")
        roles = [{"roleID": row[0], "roleName": row[1]} for row in cur.fetchall()]
        return jsonify(roles)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/admin/users", methods=['GET'])
def admin_get_users():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Join with roles and regulators to get names
        sql = """
            SELECT u.userid, u.email, r.roleid, r.rolename, reg.regulatorid, reg.name as regulatorname
            FROM users u
            JOIN roles r ON u.roleid = r.roleid
            LEFT JOIN regulators reg ON u.regulatorid = reg.regulatorid
            WHERE u.is_archived = FALSE
            ORDER BY u.email;
        """
        cur.execute(sql)
        users = [
            {"userID": row[0], "email": row[1], "roleID": row[2], "roleName": row[3], "regulatorID": row[4], "regulatorName": row[5]}
            for row in cur.fetchall()
        ]
        return jsonify(users)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/admin/users", methods=['POST'])
def admin_create_user():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    role_id = data.get('roleID')
    regulator_id = data.get('regulatorID') # Can be None

    if not email or not password or not role_id:
        return jsonify({"error": "Email, password, and role are required."}), 400
    
    password_hash = generate_password_hash(password)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        sql = """
            INSERT INTO users (email, passwordhash, roleid, regulatorid)
            VALUES (%s, %s, %s, %s) RETURNING userid;
        """
        cur.execute(sql, (email, password_hash, role_id, regulator_id))
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"success": True, "message": "User created", "userID": new_id}), 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "A user with this email already exists."}), 409
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/admin/users/<int:user_id>", methods=['PUT'])
def admin_update_user(user_id):
    data = request.get_json()
    email = data.get('email')
    role_id = data.get('roleID')
    regulator_id = data.get('regulatorID')
    password = data.get('password') # Optional

    if not email or not role_id:
        return jsonify({"error": "Email and role are required."}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if password:
            # If password is provided, update it
            password_hash = generate_password_hash(password)
            sql = """
                UPDATE users SET email = %s, roleid = %s, regulatorid = %s, passwordhash = %s
                WHERE userid = %s;
            """
            cur.execute(sql, (email, role_id, regulator_id, password_hash, user_id))
        else:
            # Otherwise, don't update the password
            sql = "UPDATE users SET email = %s, roleid = %s, regulatorid = %s WHERE userid = %s;"
            cur.execute(sql, (email, role_id, regulator_id, user_id))
        
        conn.commit()
        return jsonify({"success": True, "message": "User updated."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/admin/users/<int:user_id>", methods=['DELETE'])
@audit_action("user_archived", target_id_param="user_id")
def admin_delete_user(user_id):
    """Performs a SOFT DELETE by archiving the user."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Set the is_archived flag to TRUE instead of deleting the row
        cur.execute("UPDATE users SET is_archived = TRUE WHERE userid = %s;", (user_id,))
        conn.commit()
        return jsonify({"success": True, "message": "User archived successfully."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

# --- Regulators ---
@app.route("/api/regulators", methods=['GET'])
def get_regulators():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT regulatorid AS id, name, abbreviation FROM regulators ORDER BY name;")
        data = cur.fetchall()
        data_list = [{"id": row[0], "name": row[1], "abbreviation": row[2]} for row in data]
        return jsonify(data_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/regulators", methods=['POST'])
def create_regulator():
    data = request.get_json()
    name = data.get('name')
    abbreviation = data.get('abbreviation')
    
    if not name or not abbreviation:
        return jsonify({"error": "Name and abbreviation are required."}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO regulators (name, abbreviation) VALUES (%s, %s) RETURNING regulatorid;", (name, abbreviation))
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": "Regulator created.", "new_regulator": {"regulatorID": new_id, "name": name, "abbreviation": abbreviation}}), 201
    except (Exception, psycopg2.DatabaseError) as error:
        if conn:
            conn.rollback()
        return jsonify({"error": str(error)}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route("/api/regulators/<int:regulator_id>", methods=['PUT'])
def update_regulator(regulator_id):
    data = request.get_json()
    name = data.get('name')
    abbreviation = data.get('abbreviation')

    if not name or not abbreviation:
        return jsonify({"error": "Both name and abbreviation are required."}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE regulators SET name = %s, abbreviation = %s WHERE regulatorid = %s;", (name, abbreviation, regulator_id))
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": "Regulator updated."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/regulators/<int:regulator_id>", methods=['DELETE'])
def delete_regulator(regulator_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM regulators WHERE regulatorid = %s;", (regulator_id,))
        conn.commit()
        return jsonify({"success": True})
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Cannot delete: regulator is linked to existing documents."}), 409
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

# --- Document Types ---
@app.route("/api/document-types", methods=['GET'])
def get_document_types():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT typeid AS id, typename AS name FROM document_types ORDER BY typename;")
        data = cur.fetchall()
        data_list = [{"id": row[0], "name": row[1]} for row in data]
        return jsonify(data_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()
        
@app.route("/api/document-types", methods=['POST'])
def create_document_type():
    data = request.get_json()
    typeName = data.get('typeName')
    if not typeName: return jsonify({"error": "typeName is required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO document_types (typename) VALUES (%s) RETURNING typeid;", (typeName,))
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"success": True, "new_document_type": {"typeID": new_id, "typeName": typeName}}), 201
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/document-types/<int:type_id>", methods=['PUT'])
def update_document_type(type_id):
    data = request.get_json()
    typeName = data.get('typeName')
    if not typeName:
        return jsonify({"error": "typeName is required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE document_types SET typename = %s WHERE typeid = %s;", (typeName, type_id))
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": "Document type updated."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/document-types/<int:type_id>", methods=['DELETE'])
def delete_document_type(type_id):
    """Performs a SOFT DELETE by archiving the document."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM document_types WHERE typeid = %s;", (type_id,))
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": "Document archived successfully."})
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Cannot delete: this type is linked to existing documents."}), 409
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

# --- User Types ---
@app.route("/api/user-types", methods=['GET'])
def get_user_types():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT usertypeid AS id, typename AS name FROM user_types ORDER BY typename;")
        data = cur.fetchall()
        data_list = [{"id": row[0], "name": row[1]} for row in data]
        return jsonify(data_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/user-types", methods=['POST'])
def create_user_type():
    """Admin endpoint to create a new user type."""
    data = request.get_json()
    typeName = data.get('typeName')

    if not typeName:
        return jsonify({"error": "typeName is required."}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        sql = "INSERT INTO user_types (typename) VALUES (%s) RETURNING usertypeid;"
        cur.execute(sql, (typeName,))
        new_id = cur.fetchone()[0]
        
        conn.commit()
        cur.close()
        
        return jsonify({"success": True, "new_user_type": {"userTypeID": new_id, "typeName": typeName}}), 201

    except psycopg2.IntegrityError:
        # This error occurs if the typeName (which is UNIQUE) already exists
        conn.rollback()
        return jsonify({"error": "This user type already exists."}), 409
    except (Exception, psycopg2.DatabaseError) as error:
        if conn:
            conn.rollback()
        return jsonify({"error": str(error)}), 500
    finally:
        if conn is not None:
            conn.close()

@app.route("/api/user-types/<int:user_type_id>", methods=['PUT'])
def update_user_type(user_type_id):
    data = request.get_json()
    typeName = data.get('typeName')
    if not typeName:
        return jsonify({"error": "typeName is required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE user_types SET typename = %s WHERE usertypeid = %s;", (typeName, user_type_id))
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": "User type updated."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/user-types/<int:user_type_id>", methods=['DELETE'])
def delete_user_type(user_type_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM user_types WHERE usertypeid = %s;", (user_type_id,))
        conn.commit()
        cur.close()
        return jsonify({"success": True})
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Cannot delete: this type is linked to existing users."}), 409
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

# --- Documents ---
@app.route("/api/documents", methods=['GET'])
def get_all_documents():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT documentid, title FROM documents ORDER BY title;")
        data = cur.fetchall()
        data_list = [{"documentID": row[0], "title": row[1]} for row in data]
        return jsonify(data_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/documents", methods=['POST'])
def create_document():
    uploader_id = session.get('user_id')
    if 'file' not in request.files: return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '' or not allowed_file(file.filename): return jsonify({"error": "Invalid file"}), 400
    
    title = request.form.get('title')
    type_id = request.form.get('typeID')
    service_ids = request.form.getlist('serviceIDs[]')
    
    text_content = extract_text_from_pdf(file) if file.filename.lower().endswith('.pdf') else ""
    ai_summary = generate_ai_summary(file)
    file.seek(0)
    
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT regulatorid FROM users WHERE userid = %s;", (uploader_id,))
        result = cur.fetchone()
        if not result or result[0] is None:
            return jsonify({"error": "Admin user not associated with a regulator."}), 403
        admin_regulator_id = result[0]

        sql_doc = "INSERT INTO documents (title, regulatorid, typeid, fileurl, uploadedby, summary_ai) VALUES (%s, %s, %s, %s, %s, %s) RETURNING documentid;"
        cur.execute(sql_doc, (title, admin_regulator_id, type_id, file_path, uploader_id, ai_summary))
        new_doc_id = cur.fetchone()[0]

        for service_id in service_ids:
            cur.execute("INSERT INTO document_services (documentid, serviceid) VALUES (%s, %s);", (new_doc_id, service_id))
        
        conn.commit()
        return jsonify({"success": True, "new_document": {"documentID": new_doc_id, "title": title}}), 201
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/documents/<int:document_id>", methods=['PUT'])
def update_document(document_id):
    data = request.get_json()
    title = data.get('title')
    if not title:
        return jsonify({"error": "Title is required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE documents SET title = %s WHERE documentid = %s;", (title, document_id))
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": "Document updated."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/documents/<int:document_id>", methods=['DELETE'])
@audit_action("document_archived", target_id_param="document_id")
def delete_document(document_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()        
        cur.execute("UPDATE documents SET is_archived = TRUE WHERE documentid = %s;", (document_id,))
        conn.commit()
        return jsonify({"success": True, "message": f"Document {document_id} deleted."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

# --- FAQs ---
@app.route("/api/faqs", methods=['GET'])
def get_all_faqs():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT faqid, question, answer FROM faqs ORDER BY question;")
        data = cur.fetchall()
        data_list = [{"faqID": row[0], "question": row[1], "answer": row[2]} for row in data]
        return jsonify(data_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/faqs", methods=['POST'])
def create_faq():
    data = request.get_json()
    question = data.get('question')
    answer = data.get('answer')
    if not question or not answer: return jsonify({"error": "Question and answer are required."}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO faqs (question, answer) VALUES (%s, %s) RETURNING faqid;", (question, answer))
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"success": True, "new_faq": {"faqID": new_id, "question": question}}), 201
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/faqs/<int:faq_id>", methods=['PUT'])
def update_faq(faq_id):
    data = request.get_json()
    question = data.get('question')
    answer = data.get('answer')
    if not question or not answer:
        return jsonify({"error": "Question and answer are required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE faqs SET question = %s, answer = %s WHERE faqid = %s;", (question, answer, faq_id))
        conn.commit()
        cur.close()
        return jsonify({"success": True, "message": "FAQ updated."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/faqs/<int:faq_id>", methods=['DELETE'])
def delete_faq(faq_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM faqs WHERE faqid = %s;", (faq_id,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# --- USER SUBSCRIPTION ENDPOINTS ---
@app.route("/api/users/<int:user_id>/subscriptions", methods=['GET'])
def get_user_subscriptions(user_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT serviceid FROM subscriptions WHERE userid = %s;", (user_id,))
        subscribed_ids = [row[0] for row in cur.fetchall()]
        return jsonify(subscribed_ids)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/users/<int:user_id>/subscriptions", methods=['POST'])
def update_user_subscriptions(user_id):
    data = request.get_json()
    service_ids = data.get('serviceIDs', [])
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM subscriptions WHERE userid = %s;", (user_id,))
        if service_ids:
            # Create a list of tuples for the executemany command
            args_list = [(user_id, service_id) for service_id in service_ids]
            cur.executemany("INSERT INTO subscriptions (userid, serviceid) VALUES (%s, %s);", args_list)
        conn.commit()
        return jsonify({"success": True, "message": "Subscriptions updated successfully."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

# === IT ADMIN ARCHIVE & RESTORE ENDPOINTS ===

@app.route("/api/admin/archive/users", methods=['GET'])
def get_archived_users():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        sql = """
            SELECT u.userid, u.email, r.rolename
            FROM users u
            JOIN roles r ON u.roleid = r.roleid
            WHERE u.is_archived = TRUE ORDER BY u.email;
        """
        cur.execute(sql)
        users = [{"userID": row[0], "email": row[1], "roleName": row[2]} for row in cur.fetchall()]
        return jsonify(users)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/admin/archive/documents", methods=['GET'])
def get_archived_documents():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT documentid, title FROM documents WHERE is_archived = TRUE ORDER BY title;")
        docs = [{"documentID": row[0], "title": row[1]} for row in cur.fetchall()]
        return jsonify(docs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/admin/restore/user/<int:user_id>", methods=['POST'])
@audit_action("user_restored", target_id_param="user_id")
def restore_user(user_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_archived = FALSE WHERE userid = %s;", (user_id,))
        conn.commit()
        return jsonify({"success": True, "message": "User restored."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/admin/restore/document/<int:document_id>", methods=['POST'])
@audit_action("document_restored", target_id_param="document_id")
def restore_document(document_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE documents SET is_archived = FALSE WHERE documentid = %s;", (document_id,))
        conn.commit()
        return jsonify({"success": True, "message": "Document restored."})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()
