from flask import Flask, request, send_file, render_template_string, redirect, url_for, flash, session, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont
import os, uuid, random, re, shutil
import pytesseract
from datetime import datetime
from ethiopian_date import EthiopianDateConverter
from functools import wraps

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-this'  # 🔐 Change this in production!

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///fayda_users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# User Model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_admin = db.Column(db.Boolean, default=False)
    generation_count = db.Column(db.Integer, default=0)

# Card Model for storing card history
class Card(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    original_filename = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    fullname = db.Column(db.String(200))
    fan_number = db.Column(db.String(50))
    
    user = db.relationship('User', backref=db.backref('cards', lazy=True))

# Initialize database
with app.app_context():
    db.create_all()
    # Create default admin user if not exists
    if not User.query.filter_by(username='admin').first():
        admin = User(
            username='admin',
            email='admin@fayda.gov.et',
            password_hash=generate_password_hash('Admin@123'),
            is_admin=True
        )
        db.session.add(admin)
        db.session.commit()

# 1. Folders
UPLOAD_FOLDER = "uploads"
IMG_FOLDER = "extracted_images"
CARD_FOLDER = "cards"
ARCHIVE_FOLDER = "card_archive"  # New folder for archive
GALLERY_FOLDER = "gallery"  # New folder for gallery view
FONT_PATH = "fonts/AbyssinicaSIL-Regular.ttf"
TEMPLATE_PATH = "static/id_card_template.png"

for folder in [UPLOAD_FOLDER, IMG_FOLDER, CARD_FOLDER, ARCHIVE_FOLDER, GALLERY_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Maaloo seensa godhaa!', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Admin required decorator
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = User.query.get(session['user_id'])
        if not user.is_admin:
            flash('Administrator ta\'uu qabda!', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def clear_old_files():
    """Clear temporary files but keep archive"""
    for folder in [UPLOAD_FOLDER, IMG_FOLDER, CARD_FOLDER]:
        for filename in os.listdir(folder):
            file_path = os.path.join(folder, filename)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")

def archive_card(card_path, user_id, original_filename="", fullname="", fan_number=""):
    """Copy card to archive and database"""
    # Generate archive filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_filename = f"card_{timestamp}_{uuid.uuid4().hex[:8]}.png"
    archive_path = os.path.join(ARCHIVE_FOLDER, archive_filename)
    
    # Copy to archive
    shutil.copy2(card_path, archive_path)
    
    # Create gallery thumbnail
    create_thumbnail(archive_path, os.path.join(GALLERY_FOLDER, archive_filename))
    
    # Save to database
    card_record = Card(
        user_id=user_id,
        filename=archive_filename,
        original_filename=original_filename,
        fullname=fullname,
        fan_number=fan_number
    )
    db.session.add(card_record)
    db.session.commit()
    
    return archive_filename

def create_thumbnail(source_path, thumb_path, size=(200, 200)):
    """Create thumbnail for gallery view"""
    try:
        img = Image.open(source_path)
        img.thumbnail(size, Image.Resampling.LANCZOS)
        img.save(thumb_path, 'PNG')
        return True
    except:
        return False

def get_user_cards(user_id, limit=50):
    """Get user's card history"""
    return Card.query.filter_by(user_id=user_id).order_by(Card.created_at.desc()).limit(limit).all()

# 2. Extract images from PDF
def extract_all_images(pdf_path):
    doc = fitz.open(pdf_path)
    image_paths = []
    
    for page_index in range(len(doc)):
        page = doc[page_index]
        image_list = page.get_images(full=True)
        
        for img_index, img in enumerate(image_list):
            xref = img[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            ext = base_image["ext"]
            
            img_name = f"page{page_index+1}_img{img_index}_{uuid.uuid4().hex[:5]}.{ext}"
            path = os.path.join(IMG_FOLDER, img_name)
            
            with open(path, "wb") as f:
                f.write(image_bytes)
            image_paths.append(path)
            
    doc.close()
    return image_paths

# 3. Extract data from PDF
def extract_pdf_data(pdf_path, image_paths):
    doc = fitz.open(pdf_path)
    page = doc[0]
    full_text = page.get_text("text")

    fin_matches = re.findall(r"\b\d{4}\s\d{4}\s\d{4}\b", full_text)
    fin_number = fin_matches[-1].strip() if fin_matches else None

    if not fin_number:
        for path in image_paths:
            if "page1_img3" in os.path.basename(path):
                try:
                    img = Image.open(path).convert('L')
                    image_text = pytesseract.image_to_string(img)
                    img_fin = re.findall(r"\b\d{4}\s\d{4}\s\d{4}\b", image_text)
                    if img_fin:
                        fin_number = img_fin[0].strip()
                        break
                except:
                    pass

    if not fin_number: fin_number = "Hin Argamne"

    fan_matches = re.findall(r"\b\d{4}\s\d{4}\s\d{4}\s\d{4}\b", full_text)
    fan_number = fan_matches[0].replace(" ", "") if fan_matches else "Hin Argamne"

    data = {
        "fullname": page.get_textbox(fitz.Rect(170.7, 218.6, 253.3, 239.2)).strip(),
        "dob": page.get_textbox(fitz.Rect(50, 290, 170, 300)).strip().replace("\n", " | "),
        "sex": page.get_textbox(fitz.Rect(50, 320, 170, 330)).strip().replace("\n", " | "),
        "nationality": page.get_textbox(fitz.Rect(50, 348, 170, 360)).strip().replace("\n", " | "),
        "phone": page.get_textbox(fitz.Rect(50, 380, 170, 400)).strip(),
        "region": page.get_textbox(fitz.Rect(150, 290, 253, 300)).strip(),
        "zone": page.get_textbox(fitz.Rect(150, 320, 320, 330)).strip(),
        "woreda": page.get_textbox(fitz.Rect(150, 350, 320, 400)).strip(),
        "fan": fan_number,
    }
    doc.close()
    return data

# 4. Generate ID Card
def generate_card(data, image_paths):
    card = Image.open(TEMPLATE_PATH).convert("RGBA")
    draw = ImageDraw.Draw(card)

    now = datetime.now()
    gc_issued = now.strftime("%d/%m/%Y")
    eth_issued_obj = EthiopianDateConverter.to_ethiopian(now.year, now.month, now.day)
    ec_issued = f"{eth_issued_obj.day:02d}/{eth_issued_obj.month:02d}/{eth_issued_obj.year}"
    
    gc_expiry = now.replace(year=now.year + 8).strftime("%d/%m/%Y")
    ec_expiry = f"{eth_issued_obj.day:02d}/{eth_issued_obj.month:02d}/{eth_issued_obj.year + 8}"
    expiry_full = f"{gc_expiry} | {ec_expiry}"

    # 4.1 Process image and remove white background
    if len(image_paths) >= 1:
        p_raw = Image.open(image_paths[0]).convert("RGBA")
        
        datas = p_raw.getdata()
        newData = []
        for item in datas:
            if item[0] > 220 and item[1] > 220 and item[2] > 220:
                newData.append((255, 255, 255, 0))
            else:
                newData.append(item)
        p_raw.putdata(newData)
        
        p_large = p_raw.resize((310, 400))
        card.paste(p_large, (65, 200), p_large)
        
        p_small = p_raw.resize((100, 135))
        card.paste(p_small, (800, 450), p_small)

    if len(image_paths) >= 2:
        s = Image.open(image_paths[1]).convert("RGBA")
        card.paste(s.resize((550, 550)), (1540, 30), s.resize((550, 550)))

    for path in image_paths:
        if "page1_img3" in os.path.basename(path):
            img3 = Image.open(path).convert("RGBA")
            crop_area = (1235, 2070, 1790, 2140) 
            img3_cropped = img3.crop(crop_area)
            img3_final = img3_cropped.resize((180,25)) 
            card.paste(img3_final, (1260, 550), img3_final) 
            break

    # 4.2 Add text
    try:
        font = ImageFont.truetype(FONT_PATH, 37)
        small = ImageFont.truetype(FONT_PATH, 32)
        iss_font = ImageFont.truetype(FONT_PATH, 25)
        sn_font = ImageFont.truetype(FONT_PATH, 26)
    except:
        font = small = iss_font = sn_font = ImageFont.load_default()

    draw.text((405, 170), data["fullname"], fill="black", font=font)
    draw.text((405, 305), data["dob"], fill="black", font=small)
    draw.text((405, 375), data["sex"], fill="black", font=small)
    draw.text((1130, 165), data["nationality"], fill="black", font=small)
    draw.text((1130, 65), data["phone"], fill="black", font=small)
    draw.text((470, 500), data["fan"], fill="black", font=small)
    draw.text((1130, 240), data["region"], fill="black", font=small)
    draw.text((1130, 315), data["zone"], fill="black", font=small)
    draw.text((1130, 390), data["woreda"], fill="black", font=small)
    draw.text((405, 440), expiry_full, fill="black", font=small)
    
    draw.text((1930, 595), f" {random.randint(10000000, 99999999)}", fill="black", font=sn_font)

    def draw_rotated_text(canvas, text, position, angle, font, color):
        text_bbox = font.getbbox(text)
        txt_img = Image.new("RGBA", (text_bbox[2], text_bbox[3] + 10), (255, 255, 255, 0))
        d = ImageDraw.Draw(txt_img)
        d.text((0, 0), text, fill=color, font=font)
        rotated = txt_img.rotate(angle, expand=True)
        canvas.paste(rotated, position, rotated)

    draw_rotated_text(card, gc_issued, (13, 120), 90, iss_font, "black")
    draw_rotated_text(card, ec_issued, (13, 390), 90, iss_font, "black")

    out_path = os.path.join(CARD_FOLDER, f"id_{uuid.uuid4().hex[:6]}.png")
    card.convert("RGB").save(out_path)
    return out_path

# HTML Templates
LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fayda ID - Seensa</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container { 
            background: white;
            border-radius: 10px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
            width: 100%;
            max-width: 400px;
            padding: 30px;
        }
        .logo { 
            text-align: center;
            margin-bottom: 20px;
            color: #2c3e50;
        }
        .logo h1 { font-size: 24px; margin-bottom: 10px; }
        .logo p { color: #7f8c8d; font-size: 14px; }
        .form-group { margin-bottom: 15px; }
        label { 
            display: block;
            margin-bottom: 5px;
            color: #2c3e50;
            font-weight: 600;
        }
        input {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 16px;
        }
        .btn {
            background: #667eea;
            color: white;
            border: none;
            padding: 12px;
            border-radius: 5px;
            font-size: 16px;
            cursor: pointer;
            width: 100%;
        }
        .alert {
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            text-align: center;
        }
        .alert-danger { background: #f8d7da; color: #721c24; }
        .alert-success { background: #d4edda; color: #155724; }
        .links { 
            text-align: center;
            margin-top: 15px;
            font-size: 14px;
        }
        .links a { 
            color: #667eea;
            text-decoration: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">
            <h1>📇 Fayda ID</h1>
            <p>Fayda ID Kaardii Uumuuf Seensa Godhaa</p>
        </div>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <form method="POST" action="{{ url_for('login') }}">
            <div class="form-group">
                <label for="username">Maqaa Seensa</label>
                <input type="text" id="username" name="username" required>
            </div>
            <div class="form-group">
                <label for="password">Iggita</label>
                <input type="password" id="password" name="password" required>
            </div>
            <button type="submit" class="btn">Seensa</button>
        </form>
        
        <div class="links">
            <p>Account hin qabdu? <a href="{{ url_for('register') }}">Galmee Godhaa</a></p>
        </div>
    </div>
</body>
</html>
'''

DASHBOARD_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - Fayda ID</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: Arial, sans-serif;
            background: #f5f5f5;
            min-height: 100vh;
        }
        .navbar {
            background: #2c3e50;
            color: white;
            padding: 15px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .navbar-brand { 
            font-size: 20px;
            font-weight: bold;
        }
        .user-info {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .logout-btn, .nav-btn {
            background: #e74c3c;
            color: white;
            border: none;
            padding: 8px 15px;
            border-radius: 5px;
            cursor: pointer;
            text-decoration: none;
            font-size: 14px;
            display: inline-block;
        }
        .nav-btn {
            background: #3498db;
            margin-right: 10px;
        }
        .nav-btn.gallery {
            background: #9b59b6;
        }
        .container {
            max-width: 1200px;
            margin: 20px auto;
            padding: 0 15px;
        }
        .welcome-card {
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        .welcome-card h2 { color: #2c3e50; margin-bottom: 10px; }
        .welcome-card p { color: #7f8c8d; }
        
        .upload-card {
            background: white;
            border-radius: 10px;
            padding: 30px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            text-align: center;
            border: 2px dashed #ddd;
            margin-bottom: 20px;
        }
        .upload-card h3 { 
            color: #2c3e50; 
            margin-bottom: 15px;
        }
        .file-input {
            margin: 15px 0;
            padding: 15px;
            border: 1px solid #ddd;
            border-radius: 5px;
            width: 100%;
        }
        .submit-btn {
            background: #27ae60;
            color: white;
            border: none;
            padding: 12px 30px;
            border-radius: 5px;
            font-size: 16px;
            cursor: pointer;
        }
        .submit-btn:disabled {
            background: #cccccc;
            cursor: not-allowed;
        }
        
        .stats-card {
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            margin-top: 20px;
        }
        .stats-card h4 { 
            color: #2c3e50; 
            margin-bottom: 15px;
        }
        .stat-item {
            display: flex;
            justify-content: space-between;
            margin: 8px 0;
            padding: 8px 0;
            border-bottom: 1px solid #f5f5f5;
        }
        .stat-value {
            font-weight: bold;
            color: #27ae60;
        }
        
        /* Gallery Preview */
        .gallery-preview {
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            margin-top: 20px;
        }
        .gallery-preview h4 { 
            color: #2c3e50; 
            margin-bottom: 15px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .cards-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }
        .card-item {
            border: 1px solid #ddd;
            border-radius: 8px;
            overflow: hidden;
            transition: transform 0.3s;
            cursor: pointer;
        }
        .card-item:hover {
            transform: scale(1.05);
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }
        .card-thumb {
            width: 100%;
            height: 120px;
            object-fit: cover;
        }
        .card-info {
            padding: 10px;
            background: #f9f9f9;
        }
        .card-name {
            font-size: 12px;
            color: #2c3e50;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-bottom: 5px;
        }
        .card-date {
            font-size: 10px;
            color: #7f8c8d;
        }
        .card-actions {
            display: flex;
            gap: 5px;
            margin-top: 5px;
        }
        .card-btn {
            padding: 3px 8px;
            border: none;
            border-radius: 3px;
            font-size: 10px;
            cursor: pointer;
            flex: 1;
        }
        .view-btn { background: #3498db; color: white; }
        .download-btn { background: #27ae60; color: white; }
        
        /* Loading Overlay */
        .loading-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .loading-content {
            background: white;
            border-radius: 10px;
            padding: 30px;
            text-align: center;
            max-width: 400px;
            width: 90%;
        }
        .spinner {
            border: 5px solid #f3f3f3;
            border-top: 5px solid #3498db;
            border-radius: 50%;
            width: 50px;
            height: 50px;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .loading-message {
            color: #2c3e50;
            font-size: 18px;
            margin-bottom: 10px;
        }
        .loading-details {
            color: #7f8c8d;
            font-size: 14px;
        }
        
        @media (max-width: 768px) {
            .container { padding: 10px; }
            .upload-card { padding: 20px; }
            .cards-grid {
                grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
            }
            .user-info {
                flex-direction: column;
                align-items: flex-end;
                gap: 5px;
            }
        }
        
        .success-check {
            color: #27ae60;
            font-size: 50px;
            margin: 20px 0;
        }
        
        .view-all-btn {
            background: #9b59b6;
            color: white;
            padding: 5px 10px;
            border-radius: 5px;
            text-decoration: none;
            font-size: 12px;
        }
    </style>
</head>
<body>
    <nav class="navbar">
        <div class="navbar-brand">📇 Fayda ID Generator</div>
        <div class="user-info">
            <a href="{{ url_for('gallery') }}" class="nav-btn gallery">📁 Kuufama</a>
            {{ user.username }}
            <a href="{{ url_for('logout') }}" class="logout-btn">Ba'i</a>
        </div>
    </nav>
    
    <div class="container">
        <div class="welcome-card">
            <h2>Baga Nagaan Dhuftan! 🎉</h2>
            <p>PDF Fayda Form filadhu fi kaardii fayda ID argadhu. Kaardiin hundi kuufama keessan (archive) keessatti galmaa'aa jira.</p>
        </div>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}" style="
                        padding: 10px;
                        border-radius: 5px;
                        margin-bottom: 15px;
                        background: {% if category=='success' %}#d4edda{% else %}#f8d7da{% endif %};
                        color: {% if category=='success' %}#155724{% else %}#721c24{% endif %};
                    ">
                        {{ message }}
                    </div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <div class="upload-card">
            <h3>📁 PDF Fayda Form filadhu</h3>
            <form method="POST" action="{{ url_for('generate_id') }}" enctype="multipart/form-data" id="uploadForm">
                <input type="file" name="pdf" class="file-input" accept=".pdf" required id="pdfFile">
                <br><br>
                <button type="submit" class="submit-btn" id="generateBtn">
                    <span id="btnText">🚀 ID Kaardii Uumu</span>
                    <span id="btnLoading" style="display: none;">
                        🔄 Uumaa jira...
                    </span>
                </button>
            </form>
            <p style="color: #666; font-size: 14px; margin-top: 10px;">
                Kaardiin kun downloads folder keessanitti bu'aa, akkasumas kuufama keessan (archive) keessatti galmaa'aa.
            </p>
        </div>
        
        <!-- Recent Cards Preview -->
        <div class="gallery-preview">
            <h4>
                <span>📋 Kaardiiwwan Dhiyoo Uumte</span>
                <a href="{{ url_for('gallery') }}" class="view-all-btn">Hunda Agarsiisi</a>
            </h4>
            {% if recent_cards %}
            <div class="cards-grid">
                {% for card in recent_cards %}
                <div class="card-item" onclick="viewCard('{{ card.filename }}')">
                    <img src="{{ url_for('get_thumbnail', filename=card.filename) }}" 
                         alt="{{ card.fullname or 'Kaardii' }}" 
                         class="card-thumb"
                         onerror="this.src='https://via.placeholder.com/150/cccccc/666666?text=No+Image'">
                    <div class="card-info">
                        <div class="card-name">{{ card.fullname or 'Kaardii ID' }}</div>
                        <div class="card-date">{{ card.created_at.strftime('%d/%m/%Y') }}</div>
                        <div class="card-actions">
                            <button class="card-btn view-btn" onclick="event.stopPropagation(); viewCard('{{ card.filename }}')">Ilaali</button>
                            <button class="card-btn download-btn" onclick="event.stopPropagation(); downloadCard('{{ card.filename }}')">Kuufi</button>
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <p style="text-align: center; color: #7f8c8d; padding: 20px;">
                Kaardiiwwan hin argamne. Kaardii uumuuf jalqabaa!
            </p>
            {% endif %}
        </div>
        
        <div class="stats-card">
            <h4>📊 Meeshaa Keessan</h4>
            <div class="stat-item">
                <span>Kaardii Uumte:</span>
                <span class="stat-value">{{ user.generation_count }}</span>
            </div>
            <div class="stat-item">
                <span>Kuufama Keessatti:</span>
                <span class="stat-value">{{ card_count }}</span>
            </div>
            <div class="stat-item">
                <span>Galmee itti godhe:</span>
                <span class="stat-value">{{ user.created_at.strftime('%d/%m/%Y') }}</span>
            </div>
        </div>
    </div>
    
    <!-- Loading Overlay -->
    <div class="loading-overlay" id="loadingOverlay">
        <div class="loading-content">
            <div class="spinner" id="loadingSpinner"></div>
            <div class="success-check" id="successCheck" style="display: none;">✅</div>
            <div class="loading-message" id="loadingMessage">Kaardii ID Uumaa Jira...</div>
            <div class="loading-details" id="loadingDetails">Maaloo eega, funyaan file-tu guddaa ta'i</div>
            <div class="progress-container" style="margin-top: 20px; width: 100%;">
                <div style="background: #f0f0f0; height: 10px; border-radius: 5px; overflow: hidden;">
                    <div id="progressBar" style="background: #3498db; height: 100%; width: 0%; transition: width 0.3s;"></div>
                </div>
                <div id="progressText" style="text-align: center; margin-top: 5px; color: #666; font-size: 14px;">0%</div>
            </div>
            <div id="downloadLinks" style="margin-top: 20px; display: none;">
                <a id="downloadLink" style="
                    background: #27ae60;
                    color: white;
                    padding: 10px 20px;
                    border-radius: 5px;
                    text-decoration: none;
                    display: inline-block;
                    margin-right: 10px;
                ">📥 Kaardii Kuufi</a>
                <a href="{{ url_for('gallery') }}" style="
                    background: #9b59b6;
                    color: white;
                    padding: 10px 20px;
                    border-radius: 5px;
                    text-decoration: none;
                    display: inline-block;
                ">📁 Kuufama Ilaali</a>
            </div>
            <button id="closeLoadingBtn" style="
                background: #e74c3c;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                margin-top: 20px;
                cursor: pointer;
                display: none;
            ">Haa dhiifnu</button>
        </div>
    </div>
    
    <script>
        // Track if form is being submitted
        let isSubmitting = false;
        let progressInterval = null;
        let latestCardFilename = null;
        
        document.getElementById('uploadForm').addEventListener('submit', function(e) {
            if (isSubmitting) {
                e.preventDefault();
                return;
            }
            
            const fileInput = document.getElementById('pdfFile');
            if (!fileInput.files[0]) {
                alert('Maaloo PDF file filadhu!');
                e.preventDefault();
                return;
            }
            
            // Mark as submitting
            isSubmitting = true;
            
            // Show loading overlay
            const loadingOverlay = document.getElementById('loadingOverlay');
            const generateBtn = document.getElementById('generateBtn');
            const btnText = document.getElementById('btnText');
            const btnLoading = document.getElementById('btnLoading');
            
            loadingOverlay.style.display = 'flex';
            generateBtn.disabled = true;
            btnText.style.display = 'none';
            btnLoading.style.display = 'inline';
            
            // Reset UI
            document.getElementById('successCheck').style.display = 'none';
            document.getElementById('loadingSpinner').style.display = 'block';
            document.getElementById('closeLoadingBtn').style.display = 'none';
            document.getElementById('downloadLinks').style.display = 'none';
            
            // Simulate progress animation
            let progress = 0;
            const progressBar = document.getElementById('progressBar');
            const progressText = document.getElementById('progressText');
            const loadingMessage = document.getElementById('loadingMessage');
            const loadingDetails = document.getElementById('loadingDetails');
            
            const messages = [
                "PDF file banuun eegalaa...",
                "Suuraa PDF irraa baasaa jira...",
                "Odeeffannoo PDF irraa baasaa jira...",
                "Kaardii ID uumuu eegalaa...",
                "Suuraa maxxansuu jira...",
                "Barreeffama kaardii irra gahuu jira...",
                "Kaardii file kessa galchuu jira...",
                "Kuufama keessatti galmaa'aa jira..."
            ];
            
            const details = [
                "PDF file server irra deebi'aa jira",
                "Suuraa hundaa PDF irraa baasaa jira",
                "Maqaa, DOB, cinsa, fi kkf PDF irraa baasaa jira",
                "Template kaardii irratti odeeffannoo maxxansaa jira",
                "Suuraa lakkofsa (photo) kaardii irratti maxxansaa jira",
                "Maqaa, lakkoofsa, fi odeeffannoo hunda barreessaa jira",
                "Kaardii PNG file kessa galchaa jira",
                "Kaardii kuufama (archive) keessatti galmaa'aa jira"
            ];
            
            // Clear any existing interval
            if (progressInterval) {
                clearInterval(progressInterval);
            }
            
            progressInterval = setInterval(() => {
                progress += 1;
                progressBar.style.width = progress + '%';
                progressText.textContent = progress + '%';
                
                // Update messages every 12-13%
                if (progress % 13 === 0 || progress === 1) {
                    const index = Math.min(Math.floor(progress / 13), messages.length - 1);
                    loadingMessage.textContent = messages[index];
                    loadingDetails.textContent = details[index];
                }
                
                // When we reach 100%, show success and prepare for form submission
                if (progress >= 100) {
                    clearInterval(progressInterval);
                    
                    // Show success state
                    loadingMessage.textContent = "Kaardii ID sirritti uumame! ✅";
                    loadingDetails.textContent = "Kuufama keessanitti galmaa'e";
                    document.getElementById('loadingSpinner').style.display = 'none';
                    document.getElementById('successCheck').style.display = 'block';
                    
                    // Now actually submit the form via AJAX
                    submitFormViaAjax();
                }
            }, 40); // Update every 40ms for smoother animation
            
            // Prevent normal form submission - we'll handle it via AJAX
            e.preventDefault();
        });
        
        function submitFormViaAjax() {
            const form = document.getElementById('uploadForm');
            const formData = new FormData(form);
            
            fetch('/generate', {
                method: 'POST',
                body: formData
            })
            .then(response => {
                if (response.ok) {
                    return response.json();
                }
                throw new Error('Network response was not ok.');
            })
            .then(data => {
                if (data.success) {
                    latestCardFilename = data.filename;
                    
                    // Show download links
                    document.getElementById('downloadLinks').style.display = 'block';
                    const downloadLink = document.getElementById('downloadLink');
                    downloadLink.href = `/download_archive/${data.filename}`;
                    downloadLink.download = data.original_name || 'Fayda_Card.png';
                    
                    // Trigger auto-download after 1 second
                    setTimeout(() => {
                        downloadLink.click();
                    }, 1000);
                    
                    // Update UI
                    loadingMessage.textContent = "✅ Kaardii sirritti uumame!";
                    loadingDetails.textContent = "Kuufama keessanitti galmaa'e fi downloads folder keessatti argamu";
                    
                    // Show close button after a moment
                    setTimeout(() => {
                        document.getElementById('closeLoadingBtn').style.display = 'block';
                    }, 2000);
                    
                    // Reload page after 5 seconds to show new card in gallery
                    setTimeout(() => {
                        window.location.reload();
                    }, 5000);
                } else {
                    throw new Error(data.error || 'Unknown error');
                }
            })
            .catch(error => {
                console.error('Error:', error);
                
                // Show error
                loadingMessage.textContent = "❌ Dogoggora ta'e!";
                loadingDetails.textContent = error.message || "Server irraa deebii hin argamne";
                document.getElementById('loadingSpinner').style.display = 'none';
                
                // Show close button
                document.getElementById('closeLoadingBtn').style.display = 'block';
                document.getElementById('closeLoadingBtn').textContent = "Haa dhiifnu";
                
                // Reset submitting state
                isSubmitting = false;
                document.getElementById('generateBtn').disabled = false;
                document.getElementById('btnText').style.display = 'inline';
                document.getElementById('btnLoading').style.display = 'none';
            });
        }
        
        // Card functions for preview
        function viewCard(filename) {
            window.open(`/view_card/${filename}`, '_blank');
        }
        
        function downloadCard(filename) {
            const link = document.createElement('a');
            link.href = `/download_archive/${filename}`;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }
        
        // Close loading overlay button
        document.getElementById('closeLoadingBtn').addEventListener('click', function() {
            document.getElementById('loadingOverlay').style.display = 'none';
            if (progressInterval) {
                clearInterval(progressInterval);
                progressInterval = null;
            }
            
            // Reset button if not submitting
            if (!isSubmitting) {
                document.getElementById('generateBtn').disabled = false;
                document.getElementById('btnText').style.display = 'inline';
                document.getElementById('btnLoading').style.display = 'none';
            }
            
            // Reload page to update gallery
            window.location.reload();
        });
        
        // Check if there was an error (from flash messages)
        window.onload = function() {
            const alerts = document.querySelectorAll('.alert');
            if (alerts.length > 0) {
                // If there's an error alert, hide loading overlay
                document.getElementById('loadingOverlay').style.display = 'none';
                document.getElementById('generateBtn').disabled = false;
                document.getElementById('btnText').style.display = 'inline';
                document.getElementById('btnLoading').style.display = 'none';
                isSubmitting = false;
            }
            
            // Clear any leftover intervals
            if (progressInterval) {
                clearInterval(progressInterval);
                progressInterval = null;
            }
        };
        
        // Prevent multiple submissions
        window.onbeforeunload = function() {
            if (isSubmitting) {
                return "Kaardii uumaa jira. Ba'anii dhiifamtaa?";
            }
        };
    </script>
</body>
</html>
'''

REGISTER_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Register - Fayda ID</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container { 
            background: white;
            border-radius: 10px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
            width: 100%;
            max-width: 400px;
            padding: 30px;
        }
        .logo { 
            text-align: center;
            margin-bottom: 20px;
            color: #2c3e50;
        }
        .logo h1 { font-size: 24px; margin-bottom: 10px; }
        .logo p { color: #7f8c8d; font-size: 14px; }
        .form-group { margin-bottom: 15px; }
        label { 
            display: block;
            margin-bottom: 5px;
            color: #2c3e50;
            font-weight: 600;
        }
        input {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 16px;
        }
        .btn {
            background: #27ae60;
            color: white;
            border: none;
            padding: 12px;
            border-radius: 5px;
            font-size: 16px;
            cursor: pointer;
            width: 100%;
        }
        .alert {
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            text-align: center;
        }
        .alert-danger { background: #f8d7da; color: #721c24; }
        .links { 
            text-align: center;
            margin-top: 15px;
            font-size: 14px;
        }
        .links a { 
            color: #667eea;
            text-decoration: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">
            <h1>📇 Fayda ID</h1>
            <p>Galmee Account Uumuuf</p>
        </div>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <form method="POST" action="{{ url_for('register') }}">
            <div class="form-group">
                <label for="username">Maqaa Seensa</label>
                <input type="text" id="username" name="username" required>
            </div>
            <div class="form-group">
                <label for="email">Email</label>
                <input type="email" id="email" name="email" required>
            </div>
            <div class="form-group">
                <label for="password">Iggita</label>
                <input type="password" id="password" name="password" required>
            </div>
            <div class="form-group">
                <label for="confirm_password">Iggita Mirkaneessi</label>
                <input type="password" id="confirm_password" name="confirm_password" required>
            </div>
            <button type="submit" class="btn">Galmee Godhaa</button>
        </form>
        
        <div class="links">
            <p>Account qabda? <a href="{{ url_for('login') }}">Seensa</a></p>
        </div>
    </div>
</body>
</html>
'''

GALLERY_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kuufama Kaardii - Fayda ID</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: Arial, sans-serif;
            background: #f5f5f5;
            min-height: 100vh;
        }
        .navbar {
            background: #2c3e50;
            color: white;
            padding: 15px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .navbar-brand { 
            font-size: 20px;
            font-weight: bold;
        }
        .user-info {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .logout-btn, .nav-btn {
            background: #e74c3c;
            color: white;
            border: none;
            padding: 8px 15px;
            border-radius: 5px;
            cursor: pointer;
            text-decoration: none;
            font-size: 14px;
            display: inline-block;
        }
        .nav-btn {
            background: #3498db;
            margin-right: 10px;
        }
        .container {
            max-width: 1400px;
            margin: 20px auto;
            padding: 0 15px;
        }
        .header {
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 15px;
        }
        .header h1 { 
            color: #2c3e50; 
            margin: 0;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .stats {
            display: flex;
            gap: 20px;
        }
        .stat-box {
            background: #f0f0f0;
            padding: 10px 15px;
            border-radius: 5px;
            text-align: center;
        }
        .stat-number {
            font-size: 20px;
            font-weight: bold;
            color: #2c3e50;
        }
        .stat-label {
            font-size: 12px;
            color: #7f8c8d;
        }
        .search-box {
            display: flex;
            gap: 10px;
            flex: 1;
            max-width: 400px;
        }
        .search-box input {
            flex: 1;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 14px;
        }
        .search-btn {
            background: #27ae60;
            color: white;
            border: none;
            padding: 10px 15px;
            border-radius: 5px;
            cursor: pointer;
        }
        .cards-container {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 20px;
        }
        .card-item {
            background: white;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            transition: transform 0.3s;
            cursor: pointer;
        }
        .card-item:hover {
            transform: translateY(-5px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
        }
        .card-thumb {
            width: 100%;
            height: 150px;
            object-fit: cover;
            border-bottom: 1px solid #eee;
        }
        .card-info {
            padding: 15px;
        }
        .card-name {
            font-size: 14px;
            color: #2c3e50;
            font-weight: bold;
            margin-bottom: 5px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .card-details {
            font-size: 12px;
            color: #7f8c8d;
            margin-bottom: 8px;
        }
        .card-date {
            font-size: 11px;
            color: #95a5a6;
            margin-bottom: 10px;
        }
        .card-actions {
            display: flex;
            gap: 8px;
        }
        .card-btn {
            flex: 1;
            padding: 6px 0;
            border: none;
            border-radius: 3px;
            font-size: 12px;
            cursor: pointer;
            text-align: center;
            text-decoration: none;
        }
        .view-btn { background: #3498db; color: white; }
        .download-btn { background: #27ae60; color: white; }
        .delete-btn { background: #e74c3c; color: white; }
        
        .empty-state {
            text-align: center;
            padding: 50px 20px;
            color: #7f8c8d;
            grid-column: 1 / -1;
        }
        .empty-state h3 {
            color: #2c3e50;
            margin-bottom: 10px;
        }
        
        @media (max-width: 768px) {
            .container { padding: 10px; }
            .header {
                flex-direction: column;
                align-items: stretch;
            }
            .search-box {
                max-width: 100%;
            }
            .cards-container {
                grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
                gap: 15px;
            }
            .user-info {
                flex-direction: column;
                align-items: flex-end;
                gap: 5px;
            }
        }
        
        .pagination {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-top: 30px;
        }
        .page-btn {
            padding: 8px 15px;
            border: 1px solid #ddd;
            background: white;
            border-radius: 5px;
            cursor: pointer;
        }
        .page-btn.active {
            background: #3498db;
            color: white;
            border-color: #3498db;
        }
        
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal-content {
            background: white;
            border-radius: 10px;
            max-width: 800px;
            width: 90%;
            max-height: 90vh;
            overflow: hidden;
        }
        .modal-header {
            padding: 20px;
            background: #2c3e50;
            color: white;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .modal-body {
            padding: 20px;
            text-align: center;
        }
        .modal-img {
            max-width: 100%;
            max-height: 60vh;
            border-radius: 5px;
        }
        .close-btn {
            background: none;
            border: none;
            color: white;
            font-size: 24px;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <nav class="navbar">
        <div class="navbar-brand">📁 Kuufama Kaardii</div>
        <div class="user-info">
            <a href="{{ url_for('dashboard') }}" class="nav-btn">← Dashboard</a>
            {{ user.username }}
            <a href="{{ url_for('logout') }}" class="logout-btn">Ba'i</a>
        </div>
    </nav>
    
    <div class="container">
        <div class="header">
            <h1>📋 Kaardiiwwan Keessan ({{ card_count }})</h1>
            <div class="stats">
                <div class="stat-box">
                    <div class="stat-number">{{ card_count }}</div>
                    <div class="stat-label">Kaardiiwwan</div>
                </div>
                <div class="stat-box">
                    <div class="stat-number">{{ total_size }}</div>
                    <div class="stat-label">MB Kuufame</div>
                </div>
            </div>
            <div class="search-box">
                <input type="text" id="searchInput" placeholder="Maqaa, FAN, ykn dates barbaadi...">
                <button class="search-btn" onclick="searchCards()">🔍 Barbaadi</button>
            </div>
        </div>
        
        {% if cards %}
        <div class="cards-container" id="cardsContainer">
            {% for card in cards %}
            <div class="card-item" data-name="{{ card.fullname or '' }}" data-fan="{{ card.fan_number or '' }}" data-date="{{ card.created_at.strftime('%Y-%m-%d') }}">
                <img src="{{ url_for('get_thumbnail', filename=card.filename) }}" 
                     alt="{{ card.fullname or 'Kaardii' }}" 
                     class="card-thumb"
                     onerror="this.src='https://via.placeholder.com/200/cccccc/666666?text=No+Image'">
                <div class="card-info">
                    <div class="card-name">{{ card.fullname or 'Kaardii ID' }}</div>
                    {% if card.fan_number %}
                    <div class="card-details">FAN: {{ card.fan_number }}</div>
                    {% endif %}
                    <div class="card-date">{{ card.created_at.strftime('%d/%m/%Y %H:%M') }}</div>
                    <div class="card-actions">
                        <button class="card-btn view-btn" onclick="viewCard('{{ card.filename }}')">Ilaali</button>
                        <a href="{{ url_for('download_archive', filename=card.filename) }}" 
                           class="card-btn download-btn" 
                           download="{{ card.original_filename or card.filename }}">Kuufi</a>
                        <button class="card-btn delete-btn" onclick="deleteCard('{{ card.id }}', '{{ card.fullname or card.filename }}')">Delete</button>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        
        {% if total_pages > 1 %}
        <div class="pagination">
            {% if page > 1 %}
            <a href="{{ url_for('gallery', page=page-1) }}" class="page-btn">← Darbee</a>
            {% endif %}
            
            {% for p in range(1, total_pages + 1) %}
                {% if p == page %}
                <span class="page-btn active">{{ p }}</span>
                {% elif p >= page-2 and p <= page+2 %}
                <a href="{{ url_for('gallery', page=p) }}" class="page-btn">{{ p }}</a>
                {% endif %}
            {% endfor %}
            
            {% if page < total_pages %}
            <a href="{{ url_for('gallery', page=page+1) }}" class="page-btn">Itaanaa →</a>
            {% endif %}
        </div>
        {% endif %}
        
        {% else %}
        <div class="empty-state">
            <h3>Kuufama keessatti kaardiiwwan hin argamne 😔</h3>
            <p>Kaardii uumuuf jalqabaa ykn PDF Fayda Form filadhu.</p>
            <a href="{{ url_for('dashboard') }}" style="
                background: #27ae60;
                color: white;
                padding: 10px 20px;
                border-radius: 5px;
                text-decoration: none;
                display: inline-block;
                margin-top: 15px;
            ">← Dashboard deebi'i</a>
        </div>
        {% endif %}
    </div>
    
    <!-- Image View Modal -->
    <div class="modal" id="imageModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 id="modalTitle">Kaardii ID</h3>
                <button class="close-btn" onclick="closeModal()">×</button>
            </div>
            <div class="modal-body">
                <img id="modalImage" class="modal-img" src="" alt="Kaardii ID">
            </div>
        </div>
    </div>
    
    <script>
        function viewCard(filename) {
            document.getElementById('modalImage').src = `/view_card/${filename}`;
            document.getElementById('modalTitle').textContent = 'Kaardii ID - ' + filename;
            document.getElementById('imageModal').style.display = 'flex';
        }
        
        function closeModal() {
            document.getElementById('imageModal').style.display = 'none';
        }
        
        function searchCards() {
            const searchTerm = document.getElementById('searchInput').value.toLowerCase();
            const cards = document.querySelectorAll('.card-item');
            
            cards.forEach(card => {
                const name = card.dataset.name.toLowerCase();
                const fan = card.dataset.fan.toLowerCase();
                const date = card.dataset.date;
                
                if (name.includes(searchTerm) || fan.includes(searchTerm) || date.includes(searchTerm)) {
                    card.style.display = 'block';
                } else {
                    card.style.display = 'none';
                }
            });
        }
        
        function deleteCard(cardId, cardName) {
            if (confirm(`Kaardii "${cardName}" delete godhuu ni barbaaddaa?`)) {
                fetch(`/delete_card/${cardId}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    }
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        alert('Kaardii delete ta\'e!');
                        window.location.reload();
                    } else {
                        alert('Dogoggora ta\'e: ' + data.error);
                    }
                })
                .catch(error => {
                    alert('Network error: ' + error);
                });
            }
        }
        
        // Close modal when clicking outside
        document.getElementById('imageModal').addEventListener('click', function(e) {
            if (e.target === this) {
                closeModal();
            }
        });
        
        // Enter key for search
        document.getElementById('searchInput').addEventListener('keyup', function(e) {
            if (e.key === 'Enter') {
                searchCards();
            }
        });
    </script>
</body>
</html>
'''

# Routes
@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            flash('Baga Nagaan Dhuftan!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Username ykn password sirrii miti!', 'danger')
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        
        if password != confirm_password:
            flash('Password waliin wal hin mirkaneessine!', 'danger')
            return render_template_string(REGISTER_TEMPLATE)
        
        if User.query.filter_by(username=username).first():
            flash('Username kun lakkoofsaa darbee jira!', 'danger')
            return render_template_string(REGISTER_TEMPLATE)
        
        if User.query.filter_by(email=email).first():
            flash('Email kun lakkoofsaa darbee jira!', 'danger')
            return render_template_string(REGISTER_TEMPLATE)
        
        hashed_password = generate_password_hash(password)
        new_user = User(
            username=username,
            email=email,
            password_hash=hashed_password
        )
        
        db.session.add(new_user)
        db.session.commit()
        
        flash('Account keessan uumame! Maaloo seenaa godhaa.', 'success')
        return redirect(url_for('login'))
    
    return render_template_string(REGISTER_TEMPLATE)

@app.route('/dashboard')
@login_required
def dashboard():
    user = User.query.get(session['user_id'])
    recent_cards = get_user_cards(user.id, limit=6)
    card_count = Card.query.filter_by(user_id=user.id).count()
    
    return render_template_string(
        DASHBOARD_TEMPLATE, 
        user=user, 
        recent_cards=recent_cards,
        card_count=card_count
    )

@app.route('/generate', methods=['POST'])
@login_required
def generate_id():
    user = User.query.get(session['user_id'])
    clear_old_files()
    
    pdf = request.files.get("pdf")
    if not pdf: 
        return jsonify({'success': False, 'error': 'Maaloo PDF filadhu!'})
    
    pdf_filename = pdf.filename
    pdf_path = os.path.join(UPLOAD_FOLDER, f"temp_{uuid.uuid4().hex[:5]}.pdf")
    pdf.save(pdf_path)
    
    try:
        all_images = extract_all_images(pdf_path)
        data = extract_pdf_data(pdf_path, all_images)
        card_path = generate_card(data, all_images)
        
        # Archive the card
        archive_filename = archive_card(
            card_path, 
            user.id, 
            original_filename=pdf_filename,
            fullname=data.get("fullname", ""),
            fan_number=data.get("fan", "")
        )
        
        # Update user generation count
        user.generation_count += 1
        db.session.commit()
        
        # Return JSON response for AJAX
        return jsonify({
            'success': True,
            'filename': archive_filename,
            'original_name': f"Fayda_Card_{datetime.now().strftime('%Y%m%d')}.png",
            'message': 'Kaardii sirritti uumame!'
        })
        
    except Exception as e:
        return jsonify({
            'success': False, 
            'error': f'Dogoggora ta\'e: {str(e)}'
        })

@app.route('/download_archive/<filename>')
@login_required
def download_archive(filename):
    """Download card from archive"""
    user_id = session['user_id']
    card = Card.query.filter_by(filename=filename, user_id=user_id).first()
    
    if not card:
        flash('Kaardii hin argamne!', 'danger')
        return redirect(url_for('dashboard'))
    
    return send_from_directory(
        ARCHIVE_FOLDER,
        filename,
        as_attachment=True,
        download_name=card.original_filename or f"Fayda_Card_{card.created_at.strftime('%Y%m%d')}.png"
    )

@app.route('/view_card/<filename>')
@login_required
def view_card(filename):
    """View card image"""
    user_id = session['user_id']
    card = Card.query.filter_by(filename=filename, user_id=user_id).first()
    
    if not card:
        return "Card not found", 404
    
    return send_from_directory(ARCHIVE_FOLDER, filename)

@app.route('/get_thumbnail/<filename>')
@login_required
def get_thumbnail(filename):
    """Get thumbnail for gallery"""
    user_id = session['user_id']
    card = Card.query.filter_by(filename=filename, user_id=user_id).first()
    
    if not card:
        # Return placeholder image
        from io import BytesIO
        img = Image.new('RGB', (200, 200), color='#cccccc')
        draw = ImageDraw.Draw(img)
        # Simple "No Image" text
        img_io = BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    
    thumb_path = os.path.join(GALLERY_FOLDER, filename)
    if os.path.exists(thumb_path):
        return send_from_directory(GALLERY_FOLDER, filename)
    else:
        # Create thumbnail on the fly
        source_path = os.path.join(ARCHIVE_FOLDER, filename)
        if create_thumbnail(source_path, thumb_path):
            return send_from_directory(GALLERY_FOLDER, filename)
        else:
            # Fallback to original
            return send_from_directory(ARCHIVE_FOLDER, filename)

@app.route('/gallery')
@login_required
def gallery():
    """Gallery page for browsing archived cards"""
    user = User.query.get(session['user_id'])
    
    # Pagination
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    cards_query = Card.query.filter_by(user_id=user.id).order_by(Card.created_at.desc())
    total_cards = cards_query.count()
    cards = cards_query.paginate(page=page, per_page=per_page)
    
    # Calculate total size
    total_size_mb = 0
    for card in cards_query.all():
        card_path = os.path.join(ARCHIVE_FOLDER, card.filename)
        if os.path.exists(card_path):
            total_size_mb += os.path.getsize(card_path) / (1024 * 1024)
    total_size = f"{total_size_mb:.1f}"
    
    return render_template_string(
        GALLERY_TEMPLATE,
        user=user,
        cards=cards.items,
        card_count=total_cards,
        total_size=total_size,
        page=page,
        total_pages=cards.pages
    )

@app.route('/delete_card/<int:card_id>', methods=['POST'])
@login_required
def delete_card(card_id):
    """Delete a card from archive"""
    user_id = session['user_id']
    card = Card.query.filter_by(id=card_id, user_id=user_id).first()
    
    if not card:
        return jsonify({'success': False, 'error': 'Card not found'})
    
    try:
        # Delete files
        archive_path = os.path.join(ARCHIVE_FOLDER, card.filename)
        thumb_path = os.path.join(GALLERY_FOLDER, card.filename)
        
        if os.path.exists(archive_path):
            os.remove(archive_path)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        
        # Delete from database
        db.session.delete(card)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Card deleted successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin')
@admin_required
def admin_dashboard():
    users = User.query.all()
    total_users = len(users)
    admin_count = sum(1 for user in users if user.is_admin)
    total_generations = sum(user.generation_count for user in users)
    
    # Build HTML table rows
    table_rows = []
    for user in users:
        table_rows.append(f'''
            <tr>
                <td>{user.id}</td>
                <td>{user.username}</td>
                <td>{user.email}</td>
                <td>{'Admin' if user.is_admin else 'User'}</td>
                <td>{user.created_at.strftime('%Y-%m-%d')}</td>
                <td>{user.generation_count}</td>
            </tr>
        ''')
    
    table_html = ''.join(table_rows)
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Dashboard</title>
        <style>
            body {{ font-family: Arial; margin: 20px; }}
            .stats {{ display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap; }}
            .stat-box {{ background: #f0f0f0; padding: 20px; border-radius: 5px; min-width: 200px; }}
            .stat-number {{ font-size: 24px; font-weight: bold; color: #2c3e50; }}
            .stat-label {{ color: #7f8c8d; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
            th {{ background: #2c3e50; color: white; }}
            .back-btn {{ background: #3498db; color: white; padding: 10px 15px; border-radius: 5px; text-decoration: none; display: inline-block; margin-bottom: 20px; }}
            .logout-btn {{ background: #e74c3c; color: white; padding: 10px 15px; border-radius: 5px; text-decoration: none; float: right; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <a href="/dashboard" class="back-btn">← Back to Dashboard</a>
            </div>
            <a href="/logout" class="logout-btn">Logout</a>
        </div>
        
        <h1>👑 Admin Dashboard</h1>
        
        <div class="stats">
            <div class="stat-box">
                <div class="stat-number">{total_users}</div>
                <div class="stat-label">Total Users</div>
            </div>
            <div class="stat-box">
                <div class="stat-number">{total_generations}</div>
                <div class="stat-label">Total Generations</div>
            </div>
            <div class="stat-box">
                <div class="stat-number">{admin_count}</div>
                <div class="stat-label">Admins</div>
            </div>
        </div>
        
        <table>
            <tr>
                <th>ID</th>
                <th>Username</th>
                <th>Email</th>
                <th>Role</th>
                <th>Created</th>
                <th>Generations</th>
            </tr>
            {table_html}
        </table>
    </body>
    </html>
    '''

@app.route('/logout')
def logout():
    session.clear()
    flash('Baga baatan!', 'info')
    return redirect(url_for('login'))

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)