import os
import logging
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv

from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp
from cachetools import TTLCache
import user_agents
import json
import secrets

# Charger les variables d'environnement
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

# 🔐 CORS
ALLOWED_ORIGINS = [
    'https://tikt0k-64.web.app',
    'http://localhost:3000'
]
CORS(app, origins=ALLOWED_ORIGINS)

# ⚡ Cache
video_cache = TTLCache(maxsize=100, ttl=3600)

# 🚫 Rate limit
limiter = Limiter(
    app,
    key_func=get_remote_address,
    default_limits=["30 per minute"]
)

# 🌍 Sites autorisés
ALLOWED_SITES = [
    'youtube.com', 'youtu.be',
    'tiktok.com',
    'facebook.com', 'fb.watch',
    'instagram.com',
    'twitter.com', 'x.com'
]

# ========== API KEYS ==========
# Stockage des API keys (en mémoire, mais tu peux mettre dans .env)
API_KEYS = {
    # Format: 'api_key': {'plan': 'free/premium/pro', 'uses': 0, 'max_uses': 100}
}

# Charger les API keys depuis .env
def load_api_keys():
    """Charge les API keys depuis les variables d'environnement"""
    keys_str = os.getenv('API_KEYS', '')
    if keys_str:
        for key_info in keys_str.split(','):
            parts = key_info.split(':')
            if len(parts) == 2:
                api_key, plan = parts
                API_KEYS[api_key] = {
                    'plan': plan,
                    'uses': 0,
                    'max_uses': 1000 if plan == 'free' else 10000 if plan == 'premium' else 999999,
                    'created': datetime.now().isoformat()
                }
    
    # API key par défaut pour test
    API_KEYS['test_key_123'] = {
        'plan': 'free',
        'uses': 0,
        'max_uses': 100,
        'created': datetime.now().isoformat()
    }

load_api_keys()

# Middleware pour vérifier l'API key
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Récupérer l'API key depuis l'en-tête
        api_key = request.headers.get('X-API-Key')
        
        # Ou depuis les paramètres GET (pour tests)
        if not api_key:
            api_key = request.args.get('api_key')
        
        if not api_key:
            return jsonify({
                'error': 'API key required',
                'message': 'Ajoutez X-API-Key dans les headers'
            }), 401
        
        # Vérifier si la clé existe
        if api_key not in API_KEYS:
            return jsonify({'error': 'Invalid API key'}), 401
        
        key_info = API_KEYS[api_key]
        
        # Vérifier le nombre d'utilisations
        if key_info['uses'] >= key_info['max_uses']:
            return jsonify({'error': 'API key limit exceeded'}), 429
        
        # Incrémenter le compteur
        key_info['uses'] += 1
        
        # Ajouter les infos à kwargs
        kwargs['api_key_info'] = key_info
        kwargs['plan'] = key_info['plan']
        
        return f(*args, **kwargs)
    return decorated

# Route pour générer une nouvelle API key (admin seulement)
@app.route('/admin/generate-key', methods=['POST'])
def generate_api_key():
    # Auth simple (à améliorer)
    auth = request.authorization
    if not auth or auth.username != os.getenv('ADMIN_USER', 'admin') or auth.password != os.getenv('ADMIN_PASS', 'admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    plan = data.get('plan', 'free')
    
    if plan not in ['free', 'premium', 'pro']:
        return jsonify({'error': 'Plan must be free, premium, or pro'}), 400
    
    # Générer une clé aléatoire
    new_key = f"{plan}_{secrets.token_hex(8)}"
    
    API_KEYS[new_key] = {
        'plan': plan,
        'uses': 0,
        'max_uses': 1000 if plan == 'free' else 10000 if plan == 'premium' else 999999,
        'created': datetime.now().isoformat()
    }
    
    return jsonify({
        'api_key': new_key,
        'plan': plan,
        'max_uses': API_KEYS[new_key]['max_uses']
    })

# Route pour voir ses infos (avec sa clé)
@app.route('/key-info', methods=['GET'])
@require_api_key
def key_info(api_key_info, plan):
    return jsonify({
        'plan': plan,
        'uses': api_key_info['uses'],
        'remaining': api_key_info['max_uses'] - api_key_info['uses'],
        'max_uses': api_key_info['max_uses'],
        'created': api_key_info['created']
    })

# 📱 Détection device
def detect_device(user_agent_string):
    ua = user_agents.parse(user_agent_string)
    
    if ua.is_mobile:
        return {'type': 'mobile', 'max_height': 480, 'emoji': '📱'}
    elif ua.is_tablet:
        return {'type': 'tablet', 'max_height': 720, 'emoji': '📟'}
    else:
        return {'type': 'desktop', 'max_height': 1080, 'emoji': '💻'}

# 📥 INFO (protégé par API key)
@app.route('/info', methods=['POST'])
@limiter.limit("30 per minute")
@require_api_key
def get_info(api_key_info, plan):
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return jsonify({'error': 'URL manquante'}), 400
    
    # 🔐 Sécurité URL
    if not any(site in url for site in ALLOWED_SITES):
        return jsonify({'error': 'Site non supporté'}), 400
    
    # Vérifier le cache
    cache_key = f"info:{url}"
    if cache_key in video_cache:
        response = video_cache[cache_key]
        response['plan'] = plan
        response['remaining'] = api_key_info['max_uses'] - api_key_info['uses']
        return jsonify(response)
    
    try:
        # Options yt-dlp
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        device = detect_device(request.headers.get('User-Agent', ''))
        
        formats = []
        seen = set()
        
        # Trier par qualité
        sorted_formats = sorted(
            info.get('formats', []),
            key=lambda x: x.get('height', 0) or 0,
            reverse=True
        )
        
        for f in sorted_formats:
            if f.get('vcodec') != 'none' and f.get('height'):
                h = f['height']
                
                if h <= device['max_height'] and h not in seen:
                    if f.get('filesize'):
                        size = round(f['filesize'] / (1024 * 1024), 1)
                        size_mb = f"{size}MB"
                    else:
                        size_mb = "?"
                    
                    formats.append({
                        'label': f"{h}p",
                        'format_id': f['format_id'],
                        'size_mb': size_mb
                    })
                    
                    seen.add(h)
        
        # Audio
        formats.append({
            'label': 'Audio MP3',
            'format_id': 'audio',
            'size_mb': '~5-10MB'
        })
        
        response = {
            'title': info.get('title', 'Sans titre'),
            'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration'),
            'device': device['emoji'],
            'plan': plan,
            'remaining': api_key_info['max_uses'] - api_key_info['uses'],
            'formats': formats
        }
        
        # Mettre en cache
        video_cache[cache_key] = response
        
        return jsonify(response)
        
    except Exception as e:
        logging.error(f"❌ INFO ERROR: {e}")
        return jsonify({'error': 'Impossible de récupérer la vidéo'}), 500

# ⬇️ DOWNLOAD (protégé par API key)
@app.route('/download', methods=['POST'])
@limiter.limit("10 per minute")
@require_api_key
def download(api_key_info, plan):
    data = request.get_json()
    url = data.get('url')
    format_id = data.get('format')
    
    if not url or not format_id:
        return jsonify({'error': 'Paramètres manquants'}), 400
    
    if not any(site in url for site in ALLOWED_SITES):
        return jsonify({'error': 'Site non supporté'}), 400
    
    try:
        # Options yt-dlp
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0'
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        direct_url = None
        
        if format_id == 'audio':
            for f in info.get('formats', []):
                if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    direct_url = f.get('url')
                    break
        else:
            for f in info.get('formats', []):
                if f.get('format_id') == format_id:
                    direct_url = f.get('url')
                    break
        
        if not direct_url:
            return jsonify({'error': 'Format introuvable'}), 404
        
        logging.info(f"📥 Download {plan} | {format_id} | {url[:40]}...")
        
        # Ajouter les infos d'utilisation dans l'en-tête
        response = redirect(direct_url, 302)
        response.headers['X-Remaining'] = str(api_key_info['max_uses'] - api_key_info['uses'])
        response.headers['X-Plan'] = plan
        
        return response
        
    except Exception as e:
        logging.error(f"❌ DOWNLOAD ERROR: {e}")
        return jsonify({'error': 'Erreur téléchargement'}), 500

# ❤️ HEALTH
@app.route('/')
def health():
    return jsonify({
        'status': 'online',
        'version': '3.0',
        'cache_size': len(video_cache),
        'mode': 'with API keys',
        'endpoints': {
            'info': '/info (POST)',
            'download': '/download (POST)',
            'generate_key': '/admin/generate-key (POST)',
            'key_info': '/key-info (GET)'
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logging.basicConfig(level=logging.INFO)
    logging.info(f"🚀 Backend avec API keys démarré sur port {port}")
    app.run(host='0.0.0.0', port=port)
