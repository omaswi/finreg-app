import os, re
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
import openai
from openai import OpenAI
from io import BytesIO
import numpy as np # Import numpy
import pgvector.psycopg2
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
CORS(app, 
    supports_credentials=True,
    origins=[
        "https://finreg-app-u45785.vm.elestio.app",
        "http://localhost:8000",
        "http://127.0.0.1:8000"
        "null"
    ],
    expose_headers=["Set-Cookie","Content-Type"],
    allow_headers=["Content-Type", "Authorization", "Accept"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]
)

app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='None',  # Changed from 'Lax' to 'None'
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_COOKIE_DOMAIN='.vm.elestio.app'  # Important for subdomains
)

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
        pass # Placeholder for your existing audit log code

@app.route("/api/audit-trail", methods=['GET'])
def get_audit_trail():
    page = request.args.get('page', 1, type=int)
    per_page = 5
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    offset = (page - 1) * per_page

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        query_params = []
        where_clauses = []

        # Base query and join
        sql_base = """
            FROM audit_trail a
            LEFT JOIN users u ON a.userid = u.userid
        """

        # Add date filtering if provided
        if start_date:
            where_clauses.append("a.timestamp >= %s")
            query_params.append(start_date)
        if end_date:
            where_clauses.append("a.timestamp <= %s")
            query_params.append(end_date)
        
        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        # Get total count for pagination
        cur.execute(f"SELECT COUNT(*) {sql_base} {where_sql}", query_params)
        total_items = cur.fetchone()[0]
        total_pages = (total_items + per_page - 1) // per_page
        
        # Add pagination parameters to the list for the final query
        query_params.extend([per_page, offset])

        # Get the logs for the current page using the CORRECT column names
        sql_select = f"""
            SELECT a.auditid, a.timestamp, u.email, a.action, a.targetid, a.additional_info
            {sql_base} {where_sql}
            ORDER BY a.timestamp DESC
            LIMIT %s OFFSET %s;
        """
        cur.execute(sql_select, query_params)
        logs = [
            {"log_id": row[0], "timestamp": row[1], "email": row[2] or "System", "action": row[3], "target_id": row[4], "info": row[5]}
            for row in cur.fetchall()
        ]
        
        return jsonify({
            "logs": logs,
            "page": page,
            "total_pages": total_pages
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

audit_logger = AuditLogger(DB_CONFIG)

@app.before_request
def load_user_id_to_g():
    g.user_id = session.get('user_id')
    #print(f"BEFORE_REQUEST: g.user_id = {g.user_id}, session.get('user_id') = {session.get('user_id')}")

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

# --- NEW HELPER FUNCTIONS ---
def get_embedding(text, model="text-embedding-ada-002"):
   text = text.replace("\n", " ")
   embedding = client.embeddings.create(input=[text], model=model).data[0].embedding
   return np.array(embedding) # Return as a numpy array

def clean_text(text):
    """A simple function to clean up common text extraction errors."""
    # Corrects words that are incorrectly split by a space (e.g., "busi ness" -> "business")
    text = re.sub(r'(\w)\s{1,2}(\w)', r'\1\2', text)
    # Removes hyphenation at the end of a line (e.g., "require-\nment" -> "requirement")
    text = re.sub(r'(\w)-\n(\w)', r'\1\2', text)
    # Replaces multiple spaces or newlines with a single space
    text = re.sub(r'\s+', ' ', text).strip()
    return text

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

def chunk_text(text, max_tokens=500):
    # This is a simple chunking strategy; more advanced ones exist
    words = text.split()
    chunks = []
    current_chunk = []
    for word in words:
        current_chunk.append(word)
        if len(current_chunk) >= max_tokens:
            chunks.append(" ".join(current_chunk))
            current_chunk = []
    if current_chunk:
        chunks.append(" ".join(current_chunk))
    return chunks

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

#@app.route("/", methods=['GET'])
#def health_check():
#    return jsonify({"status": "ok", "message": "FinReg Portal API is running."})
    
@app.route("/", methods=['GET'])
def serve_index():
    return send_from_directory('frontend', 'index.html')

# This will serve any other file (like CSS or other HTML pages)
@app.route('/<path:path>')
def serve_static_files(path):
    return send_from_directory('frontend', path)

@app.route("/api/financial-services", methods=['POST'])
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
    pass

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
    pass

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
    pass

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
    pass

# === USER AUTHENTICATION & REGISTRATION ===

@app.route('/api/check-session')
def check_session():
    if 'user_id' in session:
        return jsonify({
            'isAuthenticated': True,
            'user_id': session['user_id'],  # Primary field
            'userID': session['user_id'],   # Backward compatibility
            'role': session.get('user_role')
        })
    return jsonify({'isAuthenticated': False}), 401
    
@app.route("/api/register", methods=['POST'])
def register_user():
    # Get form data
    email = request.form.get('email')
    password = request.form.get('password')
    user_type_id = request.form.get('userTypeID')
    
    if not all([email, password, user_type_id]):
        return jsonify({"error": "Email, password, and user type are required."}), 400

    file_path = None # Default to None if no file is uploaded
    if 'profilePDF' in request.files:
        file = request.files['profilePDF']
        if file and file.filename != '' and allowed_file(file.filename):
            filename = secure_filename(f"profile_{email}_{file.filename}")
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)

    # Continue with saving user to the database
    password_hash = generate_password_hash(password)
    public_user_role_id = 3 # 'Public User' role

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        sql = """
            INSERT INTO users (email, passwordhash, roleid, usertypeid, profiledetails) 
            VALUES (%s, %s, %s, %s, %s);
        """
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

# ✅ 1. NEW LOGIN REQUIRED DECORATOR
# This decorator will be used to protect specific routes.
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated_function

@app.route("/api/login", methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON data received"}), 400
            
        email = data.get('email')
        password = data.get('password')
        
        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT u.passwordhash, r.rolename, u.userid 
            FROM users u 
            JOIN roles r ON u.roleid = r.roleid 
            WHERE u.email = %s AND u.is_archived = FALSE;
        """, (email,))
        user_data = cur.fetchone()

        if user_data and check_password_hash(user_data[0], password):
            session.clear()
            session['user_id'] = user_data[2]
            session['user_role'] = user_data[1]
            session.permanent = True
            
            audit_logger.log(user_id=g.user_id, action="user_login_success", metadata={"email": email, "ip": request.remote_addr})
            return jsonify({
                "success": True,
                "message": "Login successful",
                "role": user_data[1],
                "user_id": user_data[2],  # Consistent naming
                "userID": user_data[2]   # Backward compatibility
            })
            audit_logger.log(
                user_id=user_data[2], 
                action="user_login_success", 
                metadata={"email": email, "ip": request.remote_addr}
            )
        else:
            audit_logger.log(user_id=None, action="user_login_failed", metadata={"email": email, "ip": request.remote_addr})
            return jsonify({"error": "Invalid email or password"}), 401
            
    except Exception as e:
        audit_logger.log(
            user_id=None,
            action="login_error",
            metadata={"email": email, "error": str(e), "ip": request.remote_addr}
        )
        print(f"Login error: {str(e)}")  # Debug logging
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if 'conn' in locals():
            conn.close()


@app.route("/api/logout", methods=['POST'])
def logout():
    user_id = session.get('user_id')
    if user_id:
        audit_logger.log(user_id=user_id, action="user_logout", metadata={"ip": request.remote_addr})

    # Create the response object first
    response = jsonify({"success": True, "message": "You have Logged out successfully"})
    
    # Clear the server-side session and delete the browser cookie
    session.clear()
    
    # Get the cookie name from app.config and delete the cookie
    cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')
    response.delete_cookie(
        key=cookie_name,
        path='/',
        domain=app.config.get('SESSION_COOKIE_DOMAIN')
    )
    return response

@app.route("/api/debug-session")
def debug_session():
    return jsonify({
        "session_id": session.sid,
        "session_data": dict(session),
        "headers": dict(request.headers)
    })
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

def require_role(role_name):
    """Decorator to protect routes based on user role."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if 'user_role' not in session or session['user_role'] != role_name:
                return jsonify({"error": "Unauthorized"}), 403
            return f(*args, **kwargs)
        return wrapped
    return decorator

@app.route("/api/regulator/documents", methods=['GET'])
@require_role('Regulator Editor')
def get_regulator_documents():
    """Gets documents only for the logged-in regulator admin."""
    regulator_id = session.get('regulator_id')
    if not regulator_id:
        return jsonify({"error": "User not associated with a regulator."}), 403

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Query is now filtered by the user's regulatorID
        sql = """
            SELECT d.documentid, d.title, dt.typename
            FROM documents d
            JOIN document_types dt ON d.typeid = dt.typeid
            WHERE d.regulatorid = %s AND d.is_archived = FALSE
            ORDER BY d.title;
        """
        cur.execute(sql, (regulator_id,))
        docs = [{"documentID": row[0], "title": row[1], "typeName": row[2]} for row in cur.fetchall()]
        return jsonify(docs)
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
    pass

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
    pass

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
    pass

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
    pass
        
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
    pass

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
    pass

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
    pass

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
    pass

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
    pass

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
    pass

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
    pass

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
    # You get this from the session
    uploader_id = session.get('user_id')
    if not uploader_id:
        return jsonify({"error": "Authentication required."}), 401

    # --- 1. Validate the incoming request and file ---
    if 'file' not in request.files:
        return jsonify({"error": "No file part in the request."}), 400
    file = request.files['file']
    if file.filename == '' or not allowed_file(file.filename):
        return jsonify({"error": "Invalid or no file selected."}), 400

    # --- 2. Extract and validate form data ---
    title = request.form.get('title')
    type_id = request.form.get('typeID')
    service_ids = request.form.getlist('serviceIDs[]')

    if not all([title, type_id, service_ids]):
        return jsonify({"error": "Title, type, and at least one service are required."}), 400

    # --- 3. Process the file and generate AI summary ---
    file.seek(0) # Rewind file before reading
    text_content = extract_text_from_pdf(file) if file.filename.lower().endswith('.pdf') else ""
    cleaned_text = clean_text(text_content) # Apply the cleaning function
    ai_summary = generate_ai_summary(cleaned_text) # Re-using your function name
    file.seek(0)  # Rewind file after reading for AI

    # --- 4. Save the file to disk ---
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)

    # --- 5. Save metadata to the database ---
    conn = None
    try:
        conn = get_db_connection()
        pgvector.psycopg2.register_vector(conn) # CRITICAL: Register vector type with the connection
        cur = conn.cursor()

        # Get the admin's associated regulator ID
        cur.execute("SELECT regulatorid FROM users WHERE userid = %s;", (uploader_id,))
        result = cur.fetchone()
        if not result or result[0] is None:
            return jsonify({"error": "Admin user is not associated with a regulator."}), 403
        admin_regulator_id = result[0]

        # Insert the document and get its new ID
        sql_doc = """
            INSERT INTO documents (title, regulatorid, typeid, fileurl, uploadedby, summary_ai) 
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING documentid;
        """
        cur.execute(sql_doc, (title, admin_regulator_id, int(type_id), file_path, uploader_id, ai_summary))
        
        # --- ROBUSTNESS CHECK ---
        new_doc_id_row = cur.fetchone()
        if new_doc_id_row is None:
            raise Exception("Failed to create document record in the database.")
        new_doc_id = new_doc_id_row[0]
        # --- END OF CHECK ---

        # --- NEW: Process and store embeddings ---
        text_content = extract_text_from_pdf(file)
        if cleaned_text:
            chunks = chunk_text(cleaned_text) # Use a helper to split text into chunks
            for chunk in chunks:
                embedding = get_embedding(chunk)
                cur.execute(
                    "INSERT INTO document_chunks (document_id, chunk_text, embedding) VALUES (%s, %s, %s);",
                    (new_doc_id, chunk, embedding)
                )
        # Link the new document to the selected financial services
        for service_id in service_ids:
            cur.execute("INSERT INTO document_services (documentid, serviceid) VALUES (%s, %s);", (new_doc_id, int(service_id)))
        
        conn.commit()
        return jsonify({"success": True, "message": "File uploaded successfully."}), 201

    except Exception as e:
        if conn:
            conn.rollback()
        # Log the full error to the console for debugging
        print(f"UPLOAD ERROR: {e}") 
        return jsonify({"error": f"An unexpected server error occurred: {e}"}), 500
    finally:
        if conn:
            conn.close()
    pass

# --- NEW SMART SEARCH ENDPOINT ---
@app.route("/api/smart-search", methods=['POST'])
def smart_search():
    data = request.get_json()
    query = data.get('query')
    if not query:
        return jsonify({"error": "A search query is required."}), 400

    conn = None
    try:
        # 1. Convert the user's query into a numpy array embedding
        cleaned_query = clean_text(query)
        query_embedding = get_embedding(cleaned_query)
        
        # 2. Connect and register the vector type with the connection
        conn = get_db_connection()
        pgvector.psycopg2.register_vector(conn)
        cur = conn.cursor()

        # 3. Perform the similarity search
        sql = """
            SELECT dc.chunk_text, d.title, d.documentid, (dc.embedding <=> %s) AS distance
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.documentid
            ORDER BY distance LIMIT 3;
        """
        # 4. Pass the numpy array directly to the execute function
        cur.execute(sql, (query_embedding,))
        doc_results = cur.fetchall()

        # --- 2. Search FAQs (Keyword Search) ---
        # A simple search using ILIKE for case-insensitive matching
        sql_faqs = "SELECT question, answer, faqid FROM faqs WHERE question ILIKE %s OR answer ILIKE %s LIMIT 2;"
        search_term = f"%{query}%"
        cur.execute(sql_faqs, (search_term, search_term))
        faq_results = cur.fetchall()

        # --- 3. Combine and Format Results ---
        final_results = []
        
        # Add FAQ results, marked with a type
        for row in faq_results:
            final_results.append({
                "type": "faq",
                "question": row[0],
                "answer": row[1]
            })
        
        # Add Document chunk results, marked with a type
        for row in doc_results:
            final_results.append({
                "type": "document",
                "text": row[0],
                "source_document": row[1],
                "documentID": row[2]
            })
        
        return jsonify(final_results)
    except Exception as e:
        print(f"SMART SEARCH ERROR: {str(e)}")
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
    pass

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
    pass

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
    pass

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
    pass

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
    pass

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
    pass


# --- USER SUBSCRIPTION ENDPOINTS ---
@app.route("/api/users/<int:user_id>/subscriptions", methods=['GET'])
def get_user_subscriptions(user_id):
    if session.get('user_id') != user_id:
        return jsonify({"error": "Forbidden"}), 403
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
    pass

@app.route("/api/users/<int:user_id>/subscriptions", methods=['POST','OPTIONS'])
def update_user_subscriptions(user_id):
    if request.method == 'OPTIONS':
        return jsonify({})

    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    if session.get('user_id') != user_id:
        return jsonify({"error": "Forbidden"}), 403

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
        response.headers.add('Access-Control-Allow-Origin', 'http://localhost:8000')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response
        
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
        response.headers.add('Access-Control-Allow-Origin', 'http://localhost:8000')
        return response
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
    pass

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
    pass

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
    pass

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
    pass

# === NEWS CRUD ENDPOINTS ===

@app.route("/api/news", methods=['GET'])
def get_all_news():
    """Gets all news articles, newest first, with pagination."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 5, type=int)
    offset = (page - 1) * per_page
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get total count for pagination
        cur.execute("SELECT COUNT(*) FROM news_articles;")
        total_items = cur.fetchone()[0]
        total_pages = (total_items + per_page - 1) // per_page

        # Get the requested page of articles
        sql = "SELECT article_id, title, content, publication_date FROM news_articles ORDER BY publication_date DESC LIMIT %s OFFSET %s;"
        cur.execute(sql, (per_page, offset))
        articles = [{"article_id": row[0], "title": row[1], "content": row[2], "publication_date": row[3]} for row in cur.fetchall()]
        
        return jsonify({
            "articles": articles,
            "page": page,
            "total_pages": total_pages
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/news/<int:article_id>", methods=['GET'])
def get_news_article(article_id):
    """Gets a single news article by its ID."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT title, content, publication_date FROM news_articles WHERE article_id = %s;", (article_id,))
        article = cur.fetchone()
        if article:
            return jsonify({"title": article[0], "content": article[1], "publication_date": article[2]})
        else:
            return jsonify({"error": "Article not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/news", methods=['POST'])
# @require_role(['Super Administrator', 'IT Administrator']) # Protect this route
def create_news_article():
    """Creates a new news article. Author is the logged-in admin."""
    author_id = session.get('user_id')
    data = request.get_json()
    title = data.get('title')
    content = data.get('content')

    if not title or not content:
        return jsonify({"error": "Title and content are required."}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        sql = "INSERT INTO news_articles (title, content, author_id) VALUES (%s, %s, %s) RETURNING article_id;"
        cur.execute(sql, (title, content, author_id))
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"success": True, "message": "News article posted.", "article_id": new_id}), 201
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

# === EVENTS CRUD ENDPOINTS ===

@app.route("/api/events", methods=['GET'])
def get_all_events():
    """Gets all upcoming events, ordered by date, with pagination."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 3, type=int) # Show 3 events per page
    offset = (page - 1) * per_page
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get total count of upcoming events
        cur.execute("SELECT COUNT(*) FROM events WHERE event_date >= CURRENT_TIMESTAMP;")
        total_items = cur.fetchone()[0]
        total_pages = (total_items + per_page - 1) // per_page

        # Get the requested page of events
        sql = """
            SELECT event_id, title, description, event_date, location 
            FROM events 
            WHERE event_date >= CURRENT_TIMESTAMP 
            ORDER BY event_date ASC 
            LIMIT %s OFFSET %s;
        """
        cur.execute(sql, (per_page, offset))
        events = [{"event_id": row[0], "title": row[1], "description": row[2], "event_date": row[3], "location": row[4]} for row in cur.fetchall()]
        
        return jsonify({
            "events": events,
            "page": page,
            "total_pages": total_pages
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/events/<int:event_id>", methods=['GET'])
def get_event(event_id):
    """Gets a single event by its ID."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT title, description, event_date, location FROM events WHERE event_id = %s;", (event_id,))
        event = cur.fetchone()
        if event:
            return jsonify({"title": event[0], "description": event[1], "event_date": event[2], "location": event[3]})
        else:
            return jsonify({"error": "Event not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route("/api/events", methods=['POST'])
# @require_role(['Super Administrator', 'IT Administrator']) # Protect this route
def create_event():
    """Creates a new event. Creator is the logged-in admin."""
    creator_id = session.get('user_id')
    data = request.get_json()
    title = data.get('title')
    description = data.get('description')
    event_date = data.get('event_date')
    location = data.get('location')

    if not title or not event_date:
        return jsonify({"error": "Title and event date are required."}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        sql = "INSERT INTO events (title, description, event_date, location, created_by) VALUES (%s, %s, %s, %s, %s) RETURNING event_id;"
        cur.execute(sql, (title, description, event_date, location, creator_id))
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"success": True, "message": "Event created.", "event_id": new_id}), 201
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()
