import os
import secrets
from flask import Flask, render_template, redirect, url_for, session, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from authlib.integrations.flask_client import OAuth
import google.generativeai as genai
from PIL import Image
from datetime import datetime

app = Flask(__name__)
app.secret_key = "SECRET_KEY_A_CHANGER"

# --- 1. CONFIGURATION API ---

# 🔴 REMPLACE CECI PAR TA CLÉ GEMINI
os.environ["GOOGLE_API_KEY"] = "" 
genai.configure(api_key=os.environ["GOOGLE_API_KEY"])

# ✅ TES CLÉS GOOGLE CLOUD (Déjà mises)
app.config['GOOGLE_CLIENT_ID'] = ''
app.config['GOOGLE_CLIENT_SECRET'] = ''

# BDD
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- 2. MODÈLES BDD ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True)
    name = db.Column(db.String(100))
    picture = db.Column(db.String(200))
    is_vip = db.Column(db.Boolean, default=False)
    credits = db.Column(db.Integer, default=5)
    conversations = db.relationship('Conversation', backref='author', lazy=True)

class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(100), default="Nouvelle discussion")
    date = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.relationship('Message', backref='conversation', lazy=True, cascade="all, delete-orphan")

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversation.id'), nullable=False)
    role = db.Column(db.String(10)) 
    content = db.Column(db.Text, nullable=False)
    has_image = db.Column(db.Boolean, default=False)

class AccessKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    is_used = db.Column(db.Boolean, default=False)

with app.app_context():
    db.create_all()
    # Ta clé Admin pour toi
    if not AccessKey.query.filter_by(key="CELIAN-BOSS-2026").first():
        db.session.add(AccessKey(key="CELIAN-BOSS-2026"))
        db.session.commit()

# --- 3. LOGIN ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'home'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=app.config['GOOGLE_CLIENT_ID'],
    client_secret=app.config['GOOGLE_CLIENT_SECRET'],
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# --- 4. IA GEMINI (Modèle 3 Flash) ---
MODEL_NAME = "gemini-3-flash-preview"

def ask_gemini_with_history(history_messages, new_text, new_image=None):
    try:
        try:
            model = genai.GenerativeModel(MODEL_NAME)
        except:
            model = genai.GenerativeModel("gemini-2.0-flash-exp")

        formatted_history = []
        for msg in history_messages:
            role = "user" if msg.role == "user" else "model"
            formatted_history.append({"role": role, "parts": [msg.content]})

        chat_session = model.start_chat(history=formatted_history)
        content = [new_text]
        if new_image:
            img = Image.open(new_image)
            content.append(img)

        response = chat_session.send_message(content)
        return response.text
    except Exception as e:
        return f"Erreur IA : {str(e)}"

# --- 5. ROUTES ---

@app.route('/')
def home(): return render_template('chat.html', user=current_user)

@app.route('/login')
def login(): return google.authorize_redirect(url_for('authorize', _external=True, _scheme='https'))

@app.route('/authorize')
def authorize():
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo') or google.get('userinfo').json()
        user = User.query.filter_by(email=user_info['email']).first()
        if not user:
            user = User(email=user_info['email'], name=user_info['name'], picture=user_info['picture'])
            db.session.add(user)
            db.session.commit()
        login_user(user)
        return redirect(url_for('home'))
    except: return "Erreur Login HTTPS."

@app.route('/logout')
@login_required
def logout(): logout_user(); return redirect(url_for('home'))

@app.route('/privacy')
def privacy(): return render_template('privacy.html')

@app.route('/terms')
def terms(): return render_template('terms.html')

# --- ROUTE DE VALIDATION DISCORD (CODE EXACT) ---
@app.route('/.well-known/discord')
def discord_verify():
    return "dh=b6fce85c7411681907889030de613863411d8deb"

# --- API CHAT ---
@app.route('/api/history')
@login_required
def get_history():
    chats = Conversation.query.filter_by(user_id=current_user.id).order_by(Conversation.date.desc()).all()
    return jsonify([{"id": c.id, "title": c.title} for c in chats])

@app.route('/api/load_chat/<int:chat_id>')
@login_required
def load_chat(chat_id):
    chat = Conversation.query.get_or_404(chat_id)
    if chat.user_id != current_user.id: return jsonify({"error": "Interdit"}), 403
    msgs = [{"role": m.role, "content": m.content} for m in chat.messages]
    return jsonify({"messages": msgs, "title": chat.title})

@app.route('/api/clear_history', methods=['POST'])
@login_required
def clear_history():
    Conversation.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({"status": "success"})

@app.route('/api/message', methods=['POST'])
def api_message():
    if not current_user.is_authenticated: return jsonify({"error": "login"}), 401
    if not current_user.is_vip and current_user.credits <= 0: return jsonify({"reponse": "🚫 Crédits épuisés."})

    text = request.form.get('message')
    chat_id_raw = request.form.get('chat_id')
    image = request.files.get('image')

    chat = None
    if chat_id_raw and chat_id_raw != "null":
        try: chat = Conversation.query.get(int(chat_id_raw))
        except: pass
    
    existing_messages = []
    if not chat:
        chat = Conversation(user_id=current_user.id, title=text[:30] + "...")
        db.session.add(chat)
        db.session.commit()
    else:
        existing_messages = Message.query.filter_by(conversation_id=chat.id).order_by(Message.id).all()

    response_text = ask_gemini_with_history(existing_messages, text, image)

    db.session.add(Message(conversation_id=chat.id, role='user', content=text, has_image=bool(image)))
    db.session.add(Message(conversation_id=chat.id, role='model', content=response_text))
    
    if not current_user.is_vip: current_user.credits -= 1
    db.session.commit()

    return jsonify({"reponse": response_text, "credits": current_user.credits, "chat_id": chat.id, "chat_title": chat.title})

# --- GESTION DES CLES VIP (PAYPAL) ---

@app.route('/api/generate_vip_key', methods=['POST'])
@login_required
def generate_vip_key():
    # Appelée quand PayPal confirme le paiement
    new_key = "VIP-" + secrets.token_hex(4).upper()
    db.session.add(AccessKey(key=new_key))
    db.session.commit()
    return jsonify({"key": new_key})

@app.route('/api/activate_vip', methods=['POST'])
@login_required
def activate_vip():
    key_input = request.json.get('key', '').strip()
    access_key = AccessKey.query.filter_by(key=key_input).first()
    if access_key and not access_key.is_used:
        access_key.is_used = True 
        current_user.is_vip = True 
        current_user.credits = 999999
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Clé invalide."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
