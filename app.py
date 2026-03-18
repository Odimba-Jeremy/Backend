import os
import logging
from datetime import datetime, timedelta
from functools import wraps
from dotenv import load_dotenv

from flask import Flask, request, jsonify, redirect, session
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp
from cachetools import TTLCache
import user_agents
import redis
from celery import Celery
import json

# Charger les variables d'environnement
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-key-change-en-prod')

# 🔐 Configuration Redis
redis_client = redis.Redis.from_url(
    os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    decode_responses=True
)

# ⚡ Configuration Celery
celery = Celery(
    app.name,
    broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('REDIS_URL', 'redis://localhost:6379/0')
)

# 🔐 CORS ultra sécurisé
ALLOWED_ORIGINS = [
    'https://tikt0k-64.web.app',
    'http://localhost:3000'  # pour le dev
]
CORS(app, origins=ALLOWED_ORIGINS)

# 📊 Rate limiting avec Redis
limiter = Limiter(
    app,
    key_func=get_remote_address,
    storage_uri=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
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

# 🔑 API Keys (pour monétisation)
API_KEYS = {
    'free': {'rate': '10 per minute', 'price': 0},
    'premium': {'rate': '100 per minute', 'price': 9.99},
    'pro': {'rate': 'unlimited', 'price': 29.99}
}
VALID_API_KEYS = {
    'free_123': 'free',
    'premium_456': 'premium',
    'pro_789': 'pro'
}

# 📈 Statistiques
def increment_stat(stat_name):
    """Incrémente un compteur Redis"""
    today = datetime.now().strftime('%Y-%m-%d')
    redis_client.incr(f"stat:{stat_name}:{today}")
    redis_client.expire(f"stat:{stat_name}:{today}", 86400 * 30)  # 30 jours

def get_stats(days=7):
    """Récupère les stats des derniers jours"""
    stats = {}
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        stats[date] = {
            'downloads': int(redis_client.get(f"stat:downloads:{date}") or 0),
            'info': int(redis_client.get(f"stat:info:{date}") or 0),
            'errors': int(redis_client.get(f"stat:errors:{date}") or 0),
            'unique_ips': redis_client.scard(f"stat:ips:{date}") or 0
        }
    return stats

# 🔐 Middleware API Key
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        
        if not api_key:
            return jsonify({'error': 'API key required'}), 401
        
        if api_key not in VALID_API_KEYS:
            return jsonify({'error': 'Invalid API key'}), 401
        
        plan = VALID_API_KEYS[api_key]
        kwargs['plan'] = plan
        
        # Rate limit personnalisé
        if plan != 'pro':
            limiter.limit(API_KEYS[plan]['rate'])(f)
        
        return f(*args, **kwargs)
    return decorated

# 🔄 Proxy rotation (pour éviter les blocages)
PROXIES = [
    'http://proxy1:port',
    'http://proxy2:port',
    # À configurer avec un service comme ProxyCrawl ou ScraperAPI
]

def get_proxy():
    """Retourne un proxy aléatoire"""
    if PROXIES:
        import random
        return random.choice(PROXIES)
    return None

# 📱 Détection device
def detect_device(user_agent_string):
    ua = user_agents.parse(user_agent_string)
    
    # Enregistrer l'IP unique pour les stats
    redis_client.sadd(f"stat:ips:{datetime.now().strftime('%Y-%m-%d')}", request.remote_addr)
    
    if ua.is_mobile:
        return {'type': 'mobile', 'max_height': 480, 'emoji': '📱'}
    elif ua.is_tablet:
        return {'type': 'tablet', 'max_height': 720, 'emoji': '📟'}
    else:
        return {'type': 'desktop', 'max_height': 1080, 'emoji': '💻'}

# 🎯 Tâche Celery pour téléchargement asynchrone
@celery.task(bind=True, max_retries=3)
def download_task(self, url, format_id):
    """Téléchargement en arrière-plan"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': format_id if format_id != 'audio' else 'bestaudio',
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        }
        
        if format_id == 'audio':
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
            }]
        
        # Ajouter un proxy si disponible
        proxy = get_proxy()
        if proxy:
            ydl_opts['proxy'] = proxy
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Trouver l'URL directe
            if format_id == 'audio':
                for f in info['formats']:
                    if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                        return {'url': f['url'], 'format': 'audio'}
            else:
                for f in info['formats']:
                    if f.get('format_id') == format_id:
                        return {'url': f['url'], 'format': 'video'}
        
        return {'error': 'Format not found'}
        
    except Exception as e:
        self.retry(exc=e, countdown=60)  # Réessayer après 60s

# 📥 INFO (avec cache Redis)
@app.route('/info', methods=['POST'])
@limiter.limit("30 per minute")
@require_api_key
def get_info(plan):
    data = request.get_json()
    url = data.get('url')
    
    increment_stat('info')
    
    if not url:
        return jsonify({'error': 'URL manquante'}), 400
    
    # 🔐 Sécurité URL
    if not any(site in url for site in ALLOWED_SITES):
        increment_stat('errors')
        return jsonify({'error': 'Site non supporté'}), 400
    
    # Cache Redis
    cache_key = f"info:{url}"
    cached = redis_client.get(cache_key)
    if cached:
        return jsonify(json.loads(cached))
    
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0'
            }
        }
        
        # Proxy pour les sites difficiles
        if 'tiktok' in url or 'instagram' in url:
            proxy = get_proxy()
            if proxy:
                ydl_opts['proxy'] = proxy
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        device = detect_device(request.headers.get('User-Agent', ''))
        
        formats = []
        seen = set()
        
        # Tri qualité
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
                    
                    # Vérifier si le format est disponible en direct
                    has_direct = 'url' in f
                    
                    formats.append({
                        'label': f"{h}p",
                        'format_id': f['format_id'],
                        'size_mb': size_mb,
                        'direct': has_direct
                    })
                    
                    seen.add(h)
        
        # Audio
        formats.append({
            'label': 'Audio MP3',
            'format_id': 'audio',
            'size_mb': '~5-10MB',
            'direct': True
        })
        
        response = {
            'title': info.get('title'),
            'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration'),
            'device': device['emoji'],
            'plan': plan,
            'formats': formats
        }
        
        # Cache pour 1 heure
        redis_client.setex(cache_key, 3600, json.dumps(response))
        
        return jsonify(response)
        
    except Exception as e:
        logging.error(f"❌ INFO ERROR: {e}")
        increment_stat('errors')
        return jsonify({'error': 'Impossible de récupérer la vidéo'}), 500

# ⬇️ DOWNLOAD (avec fallback)
@app.route('/download', methods=['POST'])
@require_api_key
def download(plan):
    data = request.get_json()
    url = data.get('url')
    format_id = data.get('format')
    async_mode = data.get('async', False)  # Pour les gros fichiers
    
    increment_stat('downloads')
    
    if not url or not format_id:
        increment_stat('errors')
        return jsonify({'error': 'Paramètres manquants'}), 400
    
    if not any(site in url for site in ALLOWED_SITES):
        increment_stat('errors')
        return jsonify({'error': 'Site non supporté'}), 400
    
    try:
        # Mode asynchrone pour les fichiers lourds
        if async_mode:
            task = download_task.delay(url, format_id)
            return jsonify({
                'task_id': task.id,
                'status': 'processing',
                'check_url': f"/task/{task.id}"
            })
        
        # Mode synchrone (rapide)
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0'
            }
        }
        
        # Proxy pour TikTok/Instagram
        if 'tiktok' in url or 'instagram' in url:
            proxy = get_proxy()
            if proxy:
                ydl_opts['proxy'] = proxy
        
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
            increment_stat('errors')
            return jsonify({'error': 'Format introuvable'}), 404
        
        logging.info(f"📥 Download {plan} | {format_id} | {url[:40]}")
        
        # Fallback: si redirect échoue, renvoyer l'URL
        try:
            return redirect(direct_url, 302)
        except:
            return jsonify({
                'download_url': direct_url,
                'expires_in': 3600,
                'format': format_id
            })
        
    except Exception as e:
        logging.error(f"❌ DOWNLOAD ERROR: {e}")
        increment_stat('errors')
        return jsonify({'error': 'Erreur téléchargement'}), 500

# 📊 STATS (admin uniquement)
@app.route('/admin/stats')
def admin_stats():
    auth = request.authorization
    if not auth or auth.username != os.getenv('ADMIN_USER', 'admin') or auth.password != os.getenv('ADMIN_PASS', 'admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    days = int(request.args.get('days', 7))
    stats = get_stats(days)
    
    # Top formats
    top_formats = {}
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        formats_key = f"stat:formats:{date}"
        formats_data = redis_client.hgetall(formats_key)
        for fmt, count in formats_data.items():
            top_formats[fmt] = top_formats.get(fmt, 0) + int(count)
    
    top_formats = dict(sorted(top_formats.items(), key=lambda x: x[1], reverse=True)[:10])
    
    return jsonify({
        'period': f"{days} days",
        'daily': stats,
        'total': {
            'downloads': sum(d['downloads'] for d in stats.values()),
            'info': sum(d['info'] for d in stats.values()),
            'unique_ips': sum(d['unique_ips'] for d in stats.values())
        },
        'top_formats': top_formats,
        'cache_size': redis_client.dbsize(),
        'active_plans': {
            'free': redis_client.scard('active:free') or 0,
            'premium': redis_client.scard('active:premium') or 0,
            'pro': redis_client.scard('active:pro') or 0
        }
    })

# 🔍 TASK STATUS (pour les downloads async)
@app.route('/task/<task_id>')
def task_status(task_id):
    task = download_task.AsyncResult(task_id)
    
    if task.state == 'PENDING':
        return jsonify({'status': 'pending'})
    elif task.state == 'SUCCESS':
        return jsonify({'status': 'success', 'result': task.result})
    elif task.state == 'FAILURE':
        return jsonify({'status': 'failed', 'error': str(task.info)})
    else:
        return jsonify({'status': task.state})

# 💰 PLANS (pour la monétisation)
@app.route('/plans')
def get_plans():
    return jsonify(API_KEYS)

# ❤️ HEALTH
@app.route('/')
def health():
    return jsonify({
        'status': 'online',
        'version': '3.0',
        'cache': redis_client.dbsize(),
        'mode': 'ULTIMATE',
        'features': ['redis', 'celery', 'proxy', 'api_keys', 'stats']
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logging.basicConfig(level=logging.INFO)
    logging.info("🚀 Backend ULTIME démarré")
    app.run(host='0.0.0.0', port=port)
