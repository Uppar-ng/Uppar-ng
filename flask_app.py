import os
from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager, jwt_required, create_access_token, get_jwt_identity
from werkzeug.utils import secure_filename
from whoosh.fields import Schema, TEXT, ID, DATETIME
from whoosh.index import create_in, open_dir, exists_in
from whoosh.qparser import QueryParser
from whoosh import scoring
from datetime import datetime
import PyPDF2
from epub_meta import get_epub_metadata
import python_pptx

# Initialize app
app = Flask(__name__, static_folder='static')

# PythonAnywhere specific configurations
basedir = os.path.abspath(os.path.dirname(__file__))
app.config.update({
    'SECRET_KEY': os.environ.get('SECRET_KEY', 'dev-key-123'),
    'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + os.path.join(basedir, 'library.db'),
    'SQLALCHEMY_TRACK_MODIFICATIONS': False,
    'UPLOAD_FOLDER': os.path.join(basedir, 'uploads'),
    'ALLOWED_EXTENSIONS': {'pdf', 'epub', 'docx', 'pptx'},
    'WHOOSH_BASE': os.path.join(basedir, 'whoosh_index'),
    'JWT_SECRET_KEY': os.environ.get('JWT_SECRET_KEY', 'jwt-secret-123')
})

# Initialize extensions
db = SQLAlchemy(app)
jwt = JWTManager(app)

# Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    documents = db.relationship('Document', backref='owner', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    author = db.Column(db.String(80))
    file_path = db.Column(db.String(256), nullable=False)
    file_type = db.Column(db.String(10), nullable=False)
    upload_date = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

# Search setup
def init_search_index():
    schema = Schema(
        id=ID(stored=True),
        title=TEXT(stored=True),
        author=TEXT(stored=True),
        content=TEXT,
        upload_date=DATETIME(stored=True)
    )
    if not exists_in(app.config['WHOOSH_BASE']):
        os.makedirs(app.config['WHOOSH_BASE'])
    return create_in(app.config['WHOOSH_BASE'], schema)

def get_search_index():
    return open_dir(app.config['WHOOSH_BASE'])

# Helper functions
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def extract_text(filepath, file_type):
    try:
        if file_type == 'pdf':
            with open(filepath, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                return ' '.join([page.extract_text() for page in reader.pages if page.extract_text()])
        elif file_type == 'epub':
            metadata = get_epub_metadata(filepath)
            return metadata.get('description', '')
        elif file_type in ['docx', 'pptx']:
            # Simple text extraction for presentations
            return "Presentation content not indexed"
        return ""
    except Exception as e:
        print(f"Error extracting text: {e}")
        return ""

def extract_metadata(filepath, file_type):
    try:
        if file_type == 'pdf':
            with open(filepath, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                metadata = reader.metadata
                return {
                    'title': metadata.get('/Title', os.path.basename(filepath)),
                    'author': metadata.get('/Author', 'Unknown')
                }
        elif file_type == 'epub':
            metadata = get_epub_metadata(filepath)
            return {
                'title': metadata.get('title', os.path.basename(filepath)),
                'author': metadata.get('authors', ['Unknown'])[0]
            }
        return {
            'title': os.path.basename(filepath),
            'author': 'Unknown'
        }
    except Exception as e:
        print(f"Error extracting metadata: {e}")
        return {
            'title': os.path.basename(filepath),
            'author': 'Unknown'
        }

# API Routes
@app.route('/api/auth', methods=['POST'])
def auth():
    username = request.json.get('username')
    password = request.json.get('password')
    user = User.query.filter_by(username=username).first()
    
    if not user or not user.check_password(password):
        return jsonify({"message": "Invalid credentials"}), 401
        
    access_token = create_access_token(identity=user.id)
    return jsonify(access_token=access_token), 200

@app.route('/api/documents', methods=['GET'])
@jwt_required()
def list_documents():
    user_id = get_jwt_identity()
    docs = Document.query.filter_by(user_id=user_id).all()
    return jsonify([{
        'id': doc.id,
        'title': doc.title,
        'author': doc.author,
        'file_type': doc.file_type,
        'upload_date': doc.upload_date.isoformat()
    } for doc in docs])

@app.route('/api/documents', methods=['POST'])
@jwt_required()
def upload_document():
    if 'file' not in request.files:
        return jsonify({"message": "No file uploaded"}), 400
        
    file = request.files['file']
    if file.filename == '' or not allowed_file(file.filename):
        return jsonify({"message": "Invalid file"}), 400
        
    filename = secure_filename(file.filename)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    
    file_type = filename.split('.')[-1]
    metadata = extract_metadata(filepath, file_type)
    user_id = get_jwt_identity()
    
    doc = Document(
        title=metadata['title'],
        author=metadata['author'],
        file_path=filepath,
        file_type=file_type,
        user_id=user_id
    )
    
    db.session.add(doc)
    db.session.commit()
    
    # Add to search index
    ix = get_search_index()
    writer = ix.writer()
    writer.add_document(
        id=str(doc.id),
        title=doc.title,
        author=doc.author or '',
        content=extract_text(filepath, file_type),
        upload_date=doc.upload_date
    )
    writer.commit()
    
    return jsonify({
        'id': doc.id,
        'title': doc.title,
        'author': doc.author
    }), 201

@app.route('/api/documents/<int:doc_id>', methods=['GET'])
@jwt_required()
def get_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if doc.user_id != get_jwt_identity():
        return jsonify({"message": "Unauthorized"}), 403
        
    return jsonify({
        'id': doc.id,
        'title': doc.title,
        'author': doc.author,
        'file_type': doc.file_type,
        'upload_date': doc.upload_date.isoformat(),
        'download_url': f'/api/documents/{doc.id}/download'
    })

@app.route('/api/documents/<int:doc_id>/download', methods=['GET'])
@jwt_required()
def download_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if doc.user_id != get_jwt_identity():
        return jsonify({"message": "Unauthorized"}), 403
        
    return send_from_directory(
        directory=os.path.dirname(doc.file_path),
        path=os.path.basename(doc.file_path),
        as_attachment=True
    )

@app.route('/api/search', methods=['GET'])
@jwt_required()
def search():
    query = request.args.get('q', '')
    if not query:
        return jsonify({"message": "Query parameter 'q' is required"}), 400
        
    ix = get_search_index()
    with ix.searcher(weighting=scoring.TF_IDF()) as searcher:
        qp = QueryParser("content", ix.schema)
        q = qp.parse(query)
        results = searcher.search(q, limit=10)
        
        return jsonify({
            'results': [{
                'id': hit['id'],
                'title': hit['title'],
                'author': hit['author'],
                'score': hit.score
            } for hit in results]
        })

# Static file serving
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('static', path)

# Initialization
with app.app_context():
    # Create directories
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['WHOOSH_BASE'], exist_ok=True)
    
    # Initialize database
    db.create_all()
    
    # Create search index if not exists
    if not exists_in(app.config['WHOOSH_BASE']):
        init_search_index()
    
    # Create default user if none exists
    if not User.query.first():
        default_user = User(username='admin')
        default_user.set_password('admin')
        db.session.add(default_user)
        db.session.commit()

if __name__ == '__main__':
    app.run(debug=True)