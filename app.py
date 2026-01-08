
from flask import Flask, request, jsonify
from flask_cors import CORS
from functools import wraps
import jwt
import bcrypt
from datetime import datetime, timedelta
from supabase import create_client
import os
from dotenv import load_dotenv
import logging

# Charger les variables d'environnement
load_dotenv(dotenv_path="ex.env")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})  # autorise toutes les origines

# Configuration
JWT_SECRET = os.getenv('JWT_SECRET', 'hospital_jwt_secret_2024')
JWT_EXPIRES_HOURS = 168  # 7 jours

# Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# Admin initial
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'admin@hospital.com')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'Admin123!')
ADMIN_NAME = os.getenv('ADMIN_NAME', 'Administrateur Principal')

# Initialiser Supabase
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =====================================================
# UTILITAIRES
# =====================================================

def hash_password(password):
    """Hasher un mot de passe"""
    try:
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
        return hashed.decode('utf-8')
    except Exception as e:
        logger.error(f"Erreur hash_password: {e}")
        raise

def check_password(password, hashed):
    """Vérifier un mot de passe hashé"""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception as e:
        logger.error(f"Erreur check_password: {e}")
        return False

def generate_token(user_data):
    """Générer un token JWT"""
    try:
        payload = {
            'user_id': user_data['id'],
            'email': user_data['email'],
            'role': user_data['role'],
            'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRES_HOURS)
        }
        return jwt.encode(payload, JWT_SECRET, algorithm='HS256')
    except Exception as e:
        logger.error(f"Erreur generate_token: {e}")
        raise

def verify_token(token):
    """Vérifier un token JWT"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Token expiré")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Token invalide: {e}")
        return None
    except Exception as e:
        logger.error(f"Erreur verify_token: {e}")
        return None

def token_required(f):
    """Décorateur pour vérifier le token JWT"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Vérifier dans les headers
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            if auth_header.startswith('Bearer '):
                token = auth_header.split(' ')[1]
        
        if not token:
            return jsonify({'message': 'Token manquant'}), 401
        
        # Vérifier le token
        payload = verify_token(token)
        if not payload:
            return jsonify({'message': 'Token invalide ou expiré'}), 401
        
        # Ajouter les infos utilisateur à la requête
        request.user = payload
        
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    """Décorateur pour vérifier les droits admin"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not hasattr(request, 'user') or request.user.get('role') != 'admin':
            return jsonify({'message': 'Accès refusé. Admin requis.'}), 403
        return f(*args, **kwargs)
    return decorated

# =====================================================
# INITIALISATION DE LA BASE DE DONNÉES
# =====================================================

def init_database():
    """Vérifier la connexion et créer l'admin si nécessaire"""
    try:
        logger.info("Vérification de la connexion Supabase...")
        
        # Test de connexion simple
        response = supabase.from_('users').select('id').limit(1).execute()
        logger.info(f"Connexion Supabase réussie. Structure réponse: {type(response)}")
        
        # Vérifier si l'admin existe
        response = supabase.from_('users').select('*').eq('email', ADMIN_EMAIL).execute()
        
        # Accéder aux données correctement
        if response.data and len(response.data) > 0:
            logger.info("Compte admin déjà existant")
        else:
            logger.info("Création du compte admin...")
            admin_data = {
                'email': ADMIN_EMAIL,
                'nom': ADMIN_NAME,
                'password_hash': hash_password(ADMIN_PASSWORD),
                'role': 'admin'
            }
            
            insert_response = supabase.from_('users').insert(admin_data).execute()
            
            if insert_response.data:
                logger.info("Compte admin créé avec succès")
            else:
                logger.error("Échec création compte admin")
                return False
        
        logger.info("Base de données initialisée avec succès")
        return True
        
    except Exception as e:
        logger.error(f"ERREUR initialisation base de données: {str(e)}")
        logger.error("Assurez-vous que:")
        logger.error(f"1. SUPABASE_URL est correct: {SUPABASE_URL}")
        logger.error(f"2. SUPABASE_KEY est correct: {SUPABASE_KEY[:20]}...")
        logger.error("3. Les tables 'users' et 'patients' existent dans Supabase")
        logger.error("4. La clé API a les permissions nécessaires")
        return False

# =====================================================
# ROUTES D'AUTHENTIFICATION
# =====================================================

@app.route('/api/auth/register', methods=['POST'])
def register():
    """Enregistrer un nouvel utilisateur"""
    try:
        # Vérifier le contenu JSON
        if not request.is_json:
            return jsonify({'message': 'Content-Type doit être application/json'}), 400
            
        data = request.get_json()
        logger.info(f"Tentative d'inscription: {data.get('email', 'no-email')}")
        
        # Validation
        required_fields = ['nom', 'email', 'password', 'role']
        for field in required_fields:
            if field not in data:
                logger.warning(f"Champ {field} manquant")
                return jsonify({'message': f'Champ {field} manquant'}), 400
        
        # Empêcher la création d'autres admins
        if data['role'] == 'admin':
            return jsonify({'message': 'Création de compte admin non autorisée'}), 403
        
        # Vérifier si l'email existe déjà
        response = supabase.from_('users').select('*').eq('email', data['email']).execute()
        
        if response.data and len(response.data) > 0:
            return jsonify({'message': 'Email déjà utilisé'}), 409
        
        # Créer l'utilisateur
        user_data = {
            'nom': data['nom'],
            'email': data['email'],
            'password_hash': hash_password(data['password']),
            'role': data['role']
        }
        
        logger.info(f"Insertion utilisateur: {user_data['email']}")
        result = supabase.from_('users').insert(user_data).execute()
        
        if not result.data:
            logger.error("Insertion retourne data vide")
            return jsonify({'message': 'Erreur création utilisateur'}), 500
        
        new_user = result.data[0]
        logger.info(f"Utilisateur créé: {new_user['id']}")
        
        # Générer le token
        token = generate_token({
            'id': new_user['id'],
            'email': new_user['email'],
            'role': new_user['role']
        })
        
        return jsonify({
            'message': 'Compte créé avec succès',
            'token': token,
            'user': {
                'id': new_user['id'],
                'nom': new_user['nom'],
                'email': new_user['email'],
                'role': new_user['role']
            }
        }), 201
        
    except Exception as e:
        logger.error(f"Erreur détaillée registration: {str(e)}", exc_info=True)
        return jsonify({'message': 'Erreur interne du serveur', 'error': str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def login():
    """Connexion utilisateur"""
    try:
        if not request.is_json:
            return jsonify({'message': 'Content-Type doit être application/json'}), 400
            
        data = request.get_json()
        logger.info(f"Tentative connexion: {data.get('email', 'no-email')}")
        
        # Validation
        if 'email' not in data or 'password' not in data:
            return jsonify({'message': 'Email et mot de passe requis'}), 400
        
        # Récupérer l'utilisateur
        result = supabase.from_('users').select('*').eq('email', data['email']).execute()
        
        if not result.data or len(result.data) == 0:
            return jsonify({'message': 'Email ou mot de passe incorrect'}), 401
        
        user = result.data[0]
        
        # Vérifier le mot de passe
        if not check_password(data['password'], user['password_hash']):
            return jsonify({'message': 'Email ou mot de passe incorrect'}), 401
        
        # Générer le token
        token = generate_token({
            'id': user['id'],
            'email': user['email'],
            'role': user['role']
        })
        
        return jsonify({
            'message': 'Connexion réussie',
            'token': token,
            'user': {
                'id': user['id'],
                'nom': user['nom'],
                'email': user['email'],
                'role': user['role']
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Erreur login: {str(e)}", exc_info=True)
        return jsonify({'message': 'Erreur interne du serveur'}), 500

@app.route('/api/auth/logout', methods=['POST'])
@token_required
def logout():
    """Déconnexion"""
    return jsonify({'message': 'Déconnexion réussie'}), 200

@app.route('/api/auth/me', methods=['GET'])
@token_required
def get_current_user():
    """Récupérer les infos de l'utilisateur courant"""
    try:
        user_id = request.user['user_id']
        result = supabase.from_('users').select('id, nom, email, role, created_at').eq('id', user_id).execute()
        
        if not result.data:
            return jsonify({'message': 'Utilisateur non trouvé'}), 404
        
        return jsonify({'user': result.data[0]}), 200
        
    except Exception as e:
        logger.error(f"Erreur get_current_user: {e}")
        return jsonify({'message': 'Erreur interne du serveur'}), 500

# =====================================================
# ROUTES DE DIAGNOSTIC
# =====================================================

@app.route('/api/debug/test', methods=['GET'])
def debug_test():
    """Route de débogage pour tester Supabase"""
    try:
        # Test 1: Vérifier les variables d'environnement
        env_status = {
            'SUPABASE_URL': 'SET' if SUPABASE_URL else 'MISSING',
            'SUPABASE_KEY': 'SET' if SUPABASE_KEY else 'MISSING',
            'ADMIN_EMAIL': ADMIN_EMAIL
        }
        
        # Test 2: Tester la connexion Supabase
        supabase_status = "UNKNOWN"
        table_users_exists = False
        table_patients_exists = False
        
        try:
            # Tester users
            response_users = supabase.from_('users').select('count', count='exact').limit(1).execute()
            table_users_exists = True
            logger.info(f"Test users réussi: {response_users}")
            
            # Tester patients
            response_patients = supabase.from_('patients').select('count', count='exact').limit(1).execute()
            table_patients_exists = True
            logger.info(f"Test patients réussi: {response_patients}")
            
            supabase_status = "CONNECTED"
            
        except Exception as e:
            supabase_status = f"ERROR: {str(e)}"
        
        return jsonify({
            'timestamp': datetime.utcnow().isoformat(),
            'environment': env_status,
            'supabase': supabase_status,
            'tables': {
                'users': table_users_exists,
                'patients': table_patients_exists
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Erreur debug_test: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug/create-tables', methods=['POST'])
def create_tables():
    """Créer les tables manuellement (à exécuter une fois)"""
    try:
        # Cette route nécessite que vous ayez créé la fonction RPC dans Supabase
        # Ou utilisez l'interface SQL de Supabase directement
        return jsonify({
            'message': 'Veuillez créer les tables manuellement dans Supabase SQL Editor',
            'sql': """
                -- Table users
                CREATE TABLE IF NOT EXISTS users (
                    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    nom VARCHAR(255) NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(50) NOT NULL CHECK (role IN ('accueil', 'docteur', 'medecin', 'pharmacie', 'facturation', 'admin')),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                -- Table patients
                CREATE TABLE IF NOT EXISTS patients (
                    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                    nom_complet VARCHAR(255) NOT NULL,
                    date_naissance DATE NOT NULL,
                    telephone VARCHAR(50),
                    email VARCHAR(255),
                    adresse TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    created_by UUID REFERENCES users(id)
                );
            """
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =====================================================
# ROUTES PATIENTS (simplifiées pour test)
# =====================================================

@app.route('/api/patients', methods=['GET'])
@token_required
def get_patients():
    """Récupérer tous les patients"""
    try:
        result = supabase.from_('patients').select('*').order('created_at', desc=True).execute()
        return jsonify({'patients': result.data}), 200
    except Exception as e:
        logger.error(f"Erreur get_patients: {e}")
        return jsonify({'message': 'Erreur interne du serveur'}), 500

@app.route('/api/patients', methods=['POST'])
@token_required
def create_patient():
    """Créer un nouveau patient"""
    try:
        data = request.get_json()
        
        required_fields = ['nom_complet', 'date_naissance']
        for field in required_fields:
            if field not in data:
                return jsonify({'message': f'Champ {field} manquant'}), 400
        
        patient_data = {
            'nom_complet': data['nom_complet'],
            'date_naissance': data['date_naissance'],
            'telephone': data.get('telephone'),
            'email': data.get('email'),
            'adresse': data.get('adresse'),
            'created_by': request.user['user_id']
        }
        
        result = supabase.from_('patients').insert(patient_data).execute()
        
        if not result.data:
            return jsonify({'message': 'Erreur création patient'}), 500
        
        return jsonify({
            'message': 'Patient créé avec succès',
            'patient': result.data[0]
        }), 201
        
    except Exception as e:
        logger.error(f"Erreur create_patient: {e}")
        return jsonify({'message': 'Erreur interne du serveur'}), 500

# =====================================================
# ROUTES SANTÉ
# =====================================================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Vérifier la santé de l'API"""
    try:
        # Tester la connexion à Supabase
        supabase.from_('users').select('count', count='exact').limit(1).execute()
        
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.utcnow().isoformat(),
            'service': 'hospital-backend',
            'version': '1.0.0'
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

# =====================================================
# CONFIGURATION ET DÉMARRAGE
# =====================================================

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("Démarrage HospitalApp Backend")
    logger.info(f"Supabase URL: {SUPABASE_URL}")
    logger.info(f"Admin email: {ADMIN_EMAIL}")
    logger.info("=" * 50)
    
    # Initialiser la base de données
    if init_database():
        logger.info("Backend HospitalApp démarré avec succès sur http://0.0.0.0:5000")
        logger.info("Routes disponibles:")
        logger.info("  GET  /api/health          - Vérifier santé API")
        logger.info("  GET  /api/debug/test      - Tester connexion Supabase")
        logger.info("  POST /api/auth/register   - S'inscrire")
        logger.info("  POST /api/auth/login      - Se connecter")
        logger.info("  GET  /api/auth/me         - Info utilisateur (token requis)")
        app.run(host='0.0.0.0', port=5000, debug=True)
    else:
        logger.error("Échec de l'initialisation. Vérifiez la configuration.")
