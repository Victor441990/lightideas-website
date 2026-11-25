from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken
import cloudinary
import cloudinary.uploader
import cloudinary.api
import datetime, json, requests, threading, os, uuid, tempfile, re, secrets, hmac
from html import escape as html_escape
from urllib.parse import urlsplit
from laptopseal_security import (
    PLANS, make_token, normalise_email, paystack_signature_is_valid,
    safe_reference, token_hash, token_prefix, verified_plan_from_transaction,
)
from owner_master_security import (
    OWNER_ACCESS_DAYS, OWNER_RATE_MAX_ATTEMPTS, OWNER_RATE_WINDOW_SECONDS,
    device_token_hash, hash_master_key, new_device_token, opaque_id,
    valid_master_key, verify_master_key,
)
from laptopseal_vendor import VENDOR_PLANS, VendorLicenseError
from laptopseal_vendor_auth import normalize_email as normalize_vendor_email
from laptopseal_vendor_payments import (
    PRODUCT_ID as VENDOR_PRODUCT_ID, issue_paid_vendor_package,
    require_vendor_payment_indexes,
)

from config import Config
from biztrack_client import BiztrackServiceError, export_csv as biztrack_export_csv, overview as biztrack_overview, registrations as biztrack_registrations
from security import LoginRateLimiter, client_address, csrf_token, validate_csrf
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.config.from_object(Config)
if app.config['TRUST_PROXY_HEADERS']:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH')
login_rate_limiter = LoginRateLimiter()

ADMIN_WRITE_ENDPOINTS = {
    'add_product', 'update_product', 'delete_product', 'upload_image',
    'upload_media', 'setup_video_preset', 'add_hero_media', 'delete_hero_media',
    'add_ad', 'update_ad', 'delete_ad', 'add_review', 'approve_review',
    'delete_review', 'add_announcement', 'update_announcement',
    'delete_announcement', 'send_bulk_email', 'laptopcare_add_manual',
    'laptopcare_mark_used', 'admin_logout', 'biztrack_export',
    'revoke_laptopseal_license', 'owner_master_key_admin',
    'revoke_owner_master_key',
}


@app.context_processor
def inject_csrf_token():
    return {'csrf_token': csrf_token}


@app.before_request
def protect_admin_state_changes():
    if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return None
    if request.endpoint == 'admin_login':
        validate_csrf()
    elif request.endpoint in ADMIN_WRITE_ENDPOINTS:
        if not session.get('admin_logged_in'):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        validate_csrf()
    return None


@app.after_request
def set_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    if request.path.startswith('/victor-admin'):
        response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    raise RuntimeError('FLASK_SECRET_KEY must be configured before the application starts.')

@app.after_request
def no_store_owner_credentials(response):
    if (request.path.startswith('/laptopseal/owner/')
            or request.path.startswith('/api/laptopseal/owner-master-key')
            or request.path == '/laptopseal/activate'):
        response.headers['Cache-Control'] = 'no-store'
    return response

# ── MongoDB
MONGO_URI = os.environ.get('MONGO_URI')
PRODUCTION_MONGO_DB_NAME = 'lightideas'
MONGO_DB_NAME_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,63}$')
_mongo_client = MongoClient(MONGO_URI)

def configured_mongo_db_name():
    """Return the production default unless a database was explicitly selected."""
    configured = os.environ.get('MONGO_DB_NAME')
    return configured.strip() if configured is not None else PRODUCTION_MONGO_DB_NAME

def laptopseal_test_database_error():
    """Fail closed when the local payment-test runner is not isolated."""
    if os.environ.get('LAPTOPSEAL_TEST_MODE') != '1':
        return None
    database_name = configured_mongo_db_name()
    if not database_name:
        return 'MONGO_DB_NAME is required for LaptopSeal payment testing.'
    if database_name.casefold() == PRODUCTION_MONGO_DB_NAME:
        return 'LaptopSeal payment testing cannot use the production database.'
    if not MONGO_DB_NAME_PATTERN.fullmatch(database_name):
        return 'MONGO_DB_NAME is not a valid isolated database name.'
    return None

def get_db():
    configuration_error = laptopseal_test_database_error()
    if configuration_error:
        raise RuntimeError(configuration_error)
    return _mongo_client[configured_mongo_db_name()]

def get_products_col():
    return get_db()['products']

def get_emails_col():
    return get_db()['emails']

def get_hero_media_col():
    return get_db()['hero_media']

def get_reviews_col():
    return get_db()['reviews']

def get_announcements_col():
    return get_db()['announcements']

def get_laptopcare_col():
    return get_db()['laptopcare_subscribers']

def get_ls_installs_col():
    return get_db()['ls_installs']

def get_ls_acks_col():
    return get_db()['ls_announcement_acks']

def get_ls_owner_key_col():
    return get_db()['ls_owner_master_key']

def get_ls_owner_activations_col():
    return get_db()['ls_owner_device_activations']

def get_ls_owner_attempts_col():
    return get_db()['ls_owner_key_attempts']

def get_ls_paystack_events_col():
    return get_db()['ls_paystack_events']

def get_ls_license_audit_col():
    """Administrative LaptopSeal license events, never activation secrets."""
    return get_db()['ls_license_audit']

def get_ls_payment_intents_col():
    return get_db()['ls_payment_intents']

def get_ads_col():
    return get_db()['ads']

# ── Cloudinary
cloudinary.config(
    cloud_name = 'dfkdvznkp',
    api_key    = '318817292118594',
    api_secret = os.environ.get('CLOUDINARY_API_SECRET')
)

# Environment configuration must win over legacy source settings. This keeps
# private Cloudinary credentials out of the running configuration.
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
)

# ── Brevo email config
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
EMAIL_SENDER  = {
    'name': os.environ.get('BREVO_SENDER_NAME', 'Light Ideas Technology'),
    'email': os.environ.get('BREVO_SENDER_EMAIL', 'info@lightideastechnology.com.ng'),
}

# New LaptopSeal credentials are one-way HMAC values.  Do not set a fallback:
# accepting a source-controlled default would recreate the former bypass.
LAPTOPSEAL_LICENSE_PEPPER = os.environ.get('LAPTOPSEAL_LICENSE_PEPPER', '')
PAYSTACK_WEBHOOK_SECRET = os.environ.get('PAYSTACK_SECRET_KEY', '')
LAPTOPSEAL_DELIVERY_KEY = os.environ.get('LAPTOPSEAL_DELIVERY_KEY', '')

def _delivery_cipher():
    if not LAPTOPSEAL_DELIVERY_KEY:
        raise RuntimeError('Secure license delivery is not configured.')
    return Fernet(LAPTOPSEAL_DELIVERY_KEY.encode('ascii'))

def _encrypt_delivery_token(token):
    return _delivery_cipher().encrypt(token.encode('utf-8')).decode('ascii')

def _decrypt_delivery_token(value):
    return _delivery_cipher().decrypt(value.encode('ascii')).decode('utf-8')

def _send_laptopseal_license_email(license_doc, token):
    plan = PLANS[license_doc['tier']]
    text = ('Your LaptopSeal activation key is: ' + token + '\n\nIt covers ' +
            str(plan['max_devices']) + ' device(s) for 30 days.')
    html = '<p>Your LaptopSeal activation key is:</p><p><strong>' + token + '</strong></p>'
    try:
        return bool(BREVO_API_KEY and send_brevo_email(license_doc['email'],
                    'Your LaptopSeal activation key', html, text))
    except Exception:
        return False

def _record_delivery_attempt(license_doc, token):
    sent = _send_laptopseal_license_email(license_doc, token)
    get_db()['ls_licenses'].update_one({'_id': license_doc['_id']}, {
        '$set': {'delivery_status': 'sent' if sent else 'pending',
                 'delivery_last_attempt_at': datetime.datetime.utcnow()},
        '$inc': {'delivery_attempts': 1}})
    return sent

def ensure_laptopseal_security_indexes():
    """Create only additive indexes. Invoke deliberately during release setup.

    It is never called by a request, so this source change cannot alter current
    production records before the owner approves a migration/release.
    """
    get_db()['ls_licenses'].create_index([('reference', ASCENDING)], unique=True,
                                         name='ls_unique_payment_reference', sparse=True)
    get_db()['ls_licenses'].create_index([('token_hash', ASCENDING)], unique=True,
                                         name='ls_unique_token_hash', sparse=True)
    get_ls_owner_attempts_col().create_index([('expires_at', ASCENDING)],
                                              expireAfterSeconds=0, name='ls_owner_attempt_expiry')
    get_ls_paystack_events_col().create_index([('event_key', ASCENDING)], unique=True,
                                              name='ls_unique_paystack_event')
    get_ls_license_audit_col().create_index([('license_id', ASCENDING), ('created_at', DESCENDING)],
                                            name='ls_license_audit_license_time')
    get_ls_payment_intents_col().create_index([('reference', ASCENDING)], unique=True,
                                              name='ls_unique_payment_intent_reference')

def _license_hash(value):
    if not LAPTOPSEAL_LICENSE_PEPPER:
        raise RuntimeError('LaptopSeal licensing is not configured.')
    return token_hash(value, LAPTOPSEAL_LICENSE_PEPPER)

def _find_laptopseal_license(raw_token):
    """New hashed tokens plus a read-only compatibility path for old records."""
    try:
        hashed = _license_hash(raw_token)
        found = get_db()['ls_licenses'].find_one({'token_hash': hashed, 'active': True})
        if found:
            return found
    except RuntimeError:
        # Existing purchased licenses remain valid until the approved migration
        # sets the pepper.  New licences and owner credentials cannot be issued.
        pass
    return get_db()['ls_licenses'].find_one({'token': raw_token, 'active': True})

def _is_known_laptopseal_key(raw_token):
    """Recognise issued customer keys, including revoked/expired records.

    This lets the unified activation route reject a known customer key through
    the customer path instead of attempting owner-key verification. No raw key
    is stored or returned by this lookup.
    """
    try:
        hashed = _license_hash(raw_token)
        if get_db()['ls_licenses'].find_one({'token_hash': hashed}):
            return True
    except RuntimeError:
        pass
    return bool(get_db()['ls_licenses'].find_one({'token': raw_token}))

# ── LaptopCare — services included each month that Victor marks off by hand
# once he's actually done them for a member. Proposed by Light based on the
# benefits already on the LaptopCare page — confirm/adjust the exact services
# and monthly limits with Victor before treating this as final.
LAPTOPCARE_PRICE_NAIRA = 5000
# Each service resets on its own clock — health checks/cleanups make sense
# monthly, but a physical servicing visit (opening the laptop, cleaning vents)
# is only worth offering every 3 months, roughly one visit per ~₦15,000 paid in.
LAPTOPCARE_SERVICES = {
    'health_check':   {'label': 'Full health check',                        'limit': 1, 'period_days': 30},
    'cleanup':        {'label': 'Software cleanup & virus removal',         'limit': 2, 'period_days': 30},
    'laptop_service': {'label': 'Laptop servicing (vents, hardware clean)', 'limit': 1, 'period_days': 90},
}


# ── Anthropic (AI chat widget)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

def product_to_dict(p):
    p['id'] = str(p['_id'])
    del p['_id']
    # Every card/thumbnail on the site and the app renders off `image`. If a
    # product only has a video (no photo), fall back to a Cloudinary frame
    # of that video so it still gets a real thumbnail instead of nothing.
    if not p.get('image') and p.get('media'):
        photo = next((m['url'] for m in p['media'] if m.get('type') == 'photo' and m.get('url')), None)
        if photo:
            p['image'] = photo
        else:
            video = next((m['url'] for m in p['media'] if m.get('type') == 'video' and m.get('url')), None)
            if video:
                p['image'] = video.rsplit('.', 1)[0] + '.jpg'
    return p

def hero_media_to_dict(m):
    m['id'] = str(m['_id'])
    del m['_id']
    if m.get('type') == 'video' and m.get('url'):
        # Cloudinary serves a frame of the video as a static image when the
        # extension is swapped to .jpg — used as the blurred slide background.
        m['poster'] = m['url'].rsplit('.', 1)[0] + '.jpg'
    return m

def ad_to_dict(a):
    a['id'] = str(a['_id'])
    del a['_id']
    if a.get('created_at') and not isinstance(a['created_at'], str):
        a['created_at'] = a['created_at'].isoformat()
    return a

def review_to_dict(r):
    r['id'] = str(r['_id'])
    del r['_id']
    return r

def announcement_to_dict(a):
    a['id'] = str(a['_id'])
    del a['_id']
    if a.get('created_at') and not isinstance(a['created_at'], str):
        a['created_at'] = a['created_at'].isoformat()
    return a

def install_to_dict(i):
    i['id'] = str(i['_id'])
    del i['_id']
    for f in ('first_seen', 'last_seen', 'activated_at'):
        if i.get(f) and not isinstance(i[f], str):
            i[f] = i[f].isoformat()
    return i

def laptopcare_to_dict(s):
    s['id'] = str(s['_id'])
    del s['_id']
    s.pop('password_hash', None)
    for f in ('start_date', 'cancelled_at'):
        if s.get(f) and not isinstance(s[f], str):
            s[f] = s[f].isoformat()
    for e in (s.get('entitlements') or {}).values():
        if isinstance(e, dict) and e.get('period_start') and not isinstance(e['period_start'], str):
            e['period_start'] = e['period_start'].isoformat()
    return s

def _as_datetime(v, fallback):
    if v is None:
        return fallback
    if isinstance(v, str):
        return datetime.datetime.fromisoformat(v)
    return v

def laptopcare_reset_if_needed(sub):
    """Each service's 'used this period' count resets on its OWN clock —
    health checks/cleanups monthly, laptop servicing quarterly — checked
    lazily whenever a member or admin looks at the record, same lazy-expiry
    pattern already used for LaptopSeal licenses, so no cron job is needed.
    Returns the (possibly updated) subscriber dict."""
    now = datetime.datetime.utcnow()
    entitlements = sub.get('entitlements') or {}
    changed = False
    for key, info in LAPTOPCARE_SERVICES.items():
        entry = entitlements.get(key)
        if isinstance(entry, dict):
            period_start = _as_datetime(entry.get('period_start'), sub.get('start_date') or now)
        else:
            # Not seen before (a brand new service, or the old flat-int
            # format from before per-service periods existed) — anchor it to
            # when the member's subscription started, not "now", so a
            # service due immediately isn't quietly pushed back a full period.
            period_start = _as_datetime(sub.get('start_date'), now)
        if (now - period_start).days >= info['period_days']:
            entitlements[key] = {'used': 0, 'period_start': now}
            changed = True
        elif not isinstance(entry, dict):
            entitlements[key] = {'used': entry if isinstance(entry, int) else 0, 'period_start': period_start}
            changed = True
    if changed:
        get_laptopcare_col().update_one({'_id': sub['_id']}, {'$set': {'entitlements': entitlements}})
        sub['entitlements'] = entitlements
    return sub
def send_brevo_email(to_email, subject, html_body, text_body):
    payload = {
        'sender':      EMAIL_SENDER,
        'to':          [{'email': to_email}],
        'subject':     subject,
        'htmlContent': html_body,
        'textContent': text_body
    }
    headers = {
        'api-key':      BREVO_API_KEY,
        'Content-Type': 'application/json'
    }
    res = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=15)
    return res.status_code in [200, 201]

def build_ai_system_prompt():
    products = list(get_products_col().find({'available': True}))
    lines = []
    for p in products:
        bits = [p.get('name', 'Unnamed')]
        price = p.get('price')
        if price:
            bits.append('₦{:,}'.format(int(price)))
        sub = p.get('sub_category', '')
        cat = p.get('category', '')
        if sub or cat:
            bits.append('(' + ' '.join(x for x in [sub, cat] if x) + ')')
        if p.get('specs'):
            bits.append('— ' + p['specs'])
        lines.append('- ' + ' '.join(bits))
    products_block = '\n'.join(lines) if lines else 'No live inventory available right now — tell the user to check /catalog or ask Victor on WhatsApp.'

    return '''You are Light, the AI assistant for Light Ideas Technology — a premium laptop refurbishing business in Lagos, Nigeria, owned by Victor.

CURRENT LIVE INVENTORY (straight from the database — this is exactly what's in stock right now):
''' + products_block + '''

Services:
1. LaptopSeal Diagnostic Tool (desktop app) — full 15-module laptop health check: Battery, Keyboard, RAM, SSD, CPU, GPU, Temperature, Ports, WiFi, Screen, Webcam, Mic, Speakers, Windows, Final Report. Pricing tiers (all last 30 days):
   - ₦2,000 — Single: 1 laptop
   - ₦8,000 — Small: up to 5 laptops
   - ₦30,000 — Medium: up to 20 laptops
   - ₦60,000 — Unlimited laptops
2. Windows Activation — contact Victor on WhatsApp.
3. MS Office Activation — contact Victor on WhatsApp.

Key pages: /catalog (browse all laptops), /laptopseal (buy & download LaptopSeal), /guide (LaptopSeal user guide), /terms (Terms & Conditions).

Contact: WhatsApp +2348169441990
Group: https://chat.whatsapp.com/GwoeaA3k8Od8SJULzc3A7N
Slogan: "Tested and Confirmed — Just for Your Convenience"

Be warm, helpful, and concise. Always guide users to Victor on WhatsApp for purchases. Only mention products and prices listed above under CURRENT LIVE INVENTORY — never invent availability or prices that aren't listed there. Keep responses short (under 80 words).'''

# ── MAIN WEBSITE
@app.route('/')
def index():
    products    = [product_to_dict(p) for p in get_products_col().find({'available': True})]
    hero_media  = [hero_media_to_dict(m) for m in get_hero_media_col().find().sort('created_at', 1)]
    hero_photos = [m for m in hero_media if m['type'] == 'photo']
    hero_videos = [m for m in hero_media if m['type'] == 'video']
    reviews     = [review_to_dict(r) for r in get_reviews_col().find({'status': {'$ne': 'pending'}}).sort('created_at', 1)]
    return render_template('index.html', products=products, hero_photos=hero_photos, hero_videos=hero_videos, reviews=reviews)

# ── CATALOG
@app.route('/catalog')
def catalog():
    products = [product_to_dict(p) for p in get_products_col().find({'available': True})]
    return render_template('catalog.html', products=products)

# ── ADMIN LOGIN
@app.route('/victor-admin', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
    error = None
    if request.method == 'POST':
        address = client_address()
        if login_rate_limiter.is_locked(address):
            error = 'Too many sign-in attempts. Please try again later.'
        elif not ADMIN_PASSWORD_HASH:
            app.logger.error('ADMIN_PASSWORD_HASH is not configured')
            error = 'Administrator sign-in is temporarily unavailable.'
        elif check_password_hash(ADMIN_PASSWORD_HASH, request.form.get('password', '')):
            session.clear()
            session.permanent = True
            session['admin_logged_in'] = True
            csrf_token()
            login_rate_limiter.clear(address)
            return redirect(url_for('admin_dashboard'))
        else:
            login_rate_limiter.register_failure(address)
            error = 'Invalid administrator credentials.'
    return render_template('admin_login.html', error=error)

# ── ADMIN DASHBOARD
@app.route('/victor-admin/dashboard')
def admin_dashboard():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    products      = [product_to_dict(p) for p in get_products_col().find()]
    emails        = [e['email'] for e in get_emails_col().find()]
    hero_media    = [hero_media_to_dict(m) for m in get_hero_media_col().find().sort('created_at', -1)]
    ads           = [ad_to_dict(a) for a in get_ads_col().find().sort('created_at', -1)]
    reviews       = [review_to_dict(r) for r in get_reviews_col().find().sort('created_at', -1)]
    announcements = [announcement_to_dict(a) for a in get_announcements_col().find().sort('created_at', -1)]
    laptopcare_raw = list(get_laptopcare_col().find().sort('start_date', -1))
    laptopcare_raw = [laptopcare_reset_if_needed(s) if s.get('status') == 'active' else s for s in laptopcare_raw]
    laptopcare    = [laptopcare_to_dict(s) for s in laptopcare_raw]

    # "Reached X of Y laptops" — Y is every laptop that has ever checked in,
    # X is how many distinct devices acked THIS announcement specifically.
    total_installs = get_ls_installs_col().count_documents({})
    for a in announcements:
        a['reached'] = get_ls_acks_col().count_documents({'announcement_id': a['id']})
        a['total_installs'] = total_installs
    return render_template('admin_dashboard.html', products=products, emails=emails, hero_media=hero_media, ads=ads, reviews=reviews, announcements=announcements, laptopcare=laptopcare, laptopcare_price=LAPTOPCARE_PRICE_NAIRA, laptopcare_services=LAPTOPCARE_SERVICES)


def require_dashboard_admin():
    if session.get('admin_logged_in'):
        return None
    return jsonify({'success': False, 'error': 'Administrator authentication is required.'}), 401


def biztrack_filters():
    return {
        'q': request.args.get('q', '')[:120],
        'willingness': request.args.get('willingness', '')[:30],
        'source': request.args.get('source', '')[:120],
    }


@app.route('/api/admin/biztrack/overview')
def biztrack_overview_api():
    denied = require_dashboard_admin()
    if denied:
        return denied
    try:
        return jsonify(biztrack_overview())
    except BiztrackServiceError:
        return jsonify({'error': 'Biztrack demand data is temporarily unavailable.'}), 502


@app.route('/api/admin/biztrack/registrations')
def biztrack_registrations_api():
    denied = require_dashboard_admin()
    if denied:
        return denied
    try:
        return jsonify(biztrack_registrations(biztrack_filters()))
    except BiztrackServiceError:
        return jsonify({'error': 'Biztrack registrations are temporarily unavailable.'}), 502


@app.route('/api/admin/biztrack/export.csv', methods=['POST'])
def biztrack_export():
    denied = require_dashboard_admin()
    if denied:
        return denied
    try:
        data = biztrack_export_csv(biztrack_filters())
    except BiztrackServiceError:
        return jsonify({'error': 'Biztrack export is temporarily unavailable.'}), 502
    return Response(data, content_type='text/csv; charset=utf-8', headers={
        'Content-Disposition': 'attachment; filename="biztrack-waitlist.csv"',
        'Cache-Control': 'no-store',
    })

# ── ADMIN LOGOUT
@app.route('/victor-admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

# ── API: Get all products
@app.route('/api/products', methods=['GET'])
def get_products():
    products = [product_to_dict(p) for p in get_products_col().find({'available': True})]
    return jsonify(products)

# ── API: Read-only catalog feed for the LightIdeas native (Android) app.
# Dedicated endpoint, separate from /api/products, so the app's contract
# doesn't break if the website's own internal API ever changes shape.
@app.route('/api/catalog', methods=['GET'])
def api_catalog():
    products = [product_to_dict(p) for p in get_products_col().find({'available': True})]
    return jsonify(products)

# ── API: Add product
@app.route('/api/products', methods=['POST'])
def add_product():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data    = request.json
    sub_cat = data.get('sub_category', '')
    if data.get('category') == 'laptop' and not sub_cat:
        sub_cat = 'budget' if int(data.get('price', 0)) <= 250000 else 'pro'
    product = {
        'name':         data.get('name', ''),
        'price':        int(data.get('price', 0)),
        'category':     data.get('category', 'laptop'),
        'sub_category': sub_cat,
        'specs':        data.get('specs', ''),
        'description':  data.get('description', ''),
        'image':        data.get('image', ''),
        'media':        data.get('media', []),
        'available':    True,
        'created_at':   datetime.datetime.now().isoformat()
    }
    result = get_products_col().insert_one(product)
    product['id'] = str(result.inserted_id)
    product.pop('_id', None)
    return jsonify({'success': True, 'product': product})

# ── API: Update product
@app.route('/api/products/<product_id>', methods=['PUT'])
def update_product(product_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data = request.json
    data.pop('id', None)
    get_products_col().update_one({'_id': ObjectId(product_id)}, {'$set': data})
    return jsonify({'success': True})

# ── API: Delete product
@app.route('/api/products/<product_id>', methods=['DELETE'])
def delete_product(product_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    get_products_col().delete_one({'_id': ObjectId(product_id)})
    return jsonify({'success': True})

# ── API: Upload image to Cloudinary
@app.route('/api/upload_image', methods=['POST'])
def upload_image():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    try:
        data    = request.json
        img_b64 = data.get('image', '')
        result  = cloudinary.uploader.upload(
            img_b64,
            folder='lightideas-products',
            transformation=[{'width': 800, 'crop': 'limit', 'quality': 'auto'}]
        )
        return jsonify({'success': True, 'url': result['secure_url']})
    except Exception as e:
        app.logger.error(f'Cloudinary upload failed: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500

# ── API: Upload hero photo/video to Cloudinary
@app.route('/api/upload_media', methods=['POST'])
def upload_media():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    try:
        file       = request.files.get('file')
        media_type = request.form.get('type', 'photo')
        if not file:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        if media_type == 'video':
            # Videos use Cloudinary's chunked upload — the plain upload() call
            # reads the whole file into memory at once and times out/fails on
            # anything but small clips. upload_large() needs a real file path
            # (it can't stream Flask's in-memory FileStorage object directly),
            # so save it to a temp file first.
            suffix = os.path.splitext(file.filename or '')[1] or '.mp4'
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = tmp.name
            file.save(tmp_path)
            try:
                result = cloudinary.uploader.upload_large(
                    tmp_path,
                    folder='lightideas-hero',
                    resource_type='video',
                    chunk_size=6000000
                )
            finally:
                os.remove(tmp_path)
        else:
            result = cloudinary.uploader.upload(file, folder='lightideas-hero', resource_type='image')
        return jsonify({'success': True, 'url': result['secure_url']})
    except Exception as e:
        app.logger.error(f'Hero media upload failed: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500

# ── One-time setup: unsigned Cloudinary presets so PRODUCT and AD videos
# upload straight from the admin's browser to Cloudinary, skipping our own
# small Render server entirely. A real one-minute video relayed through the
# server (the old path, still used for hero videos) can hit Render's request
# time limit before it ever reaches Cloudinary on a slow connection — this
# removes that failure point completely. Visit this URL once while logged
# into /victor-admin; safe to visit again, does nothing for a preset that
# already exists.
PRODUCT_VIDEO_PRESET = 'lightideas_product_video'
AD_VIDEO_PRESET = 'lightideas_ad_video'
@app.route('/api/admin/setup_video_preset', methods=['GET'])
def setup_video_preset():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    messages = []
    for preset_name, folder in ((PRODUCT_VIDEO_PRESET, 'lightideas-products'), (AD_VIDEO_PRESET, 'lightideas-ads')):
        try:
            cloudinary.api.create_upload_preset(
                name=preset_name,
                unsigned=True,
                folder=folder,
                resource_type='video'
            )
            messages.append(f'{preset_name}: created.')
        except Exception as e:
            msg = str(e).lower()
            # Cloudinary's actual wording here is "Name has already been
            # taken" (confirmed live), not "already exist" — the original
            # check only matched the wrong phrase, so it never recognised an
            # existing preset and bailed out of the whole loop on the very
            # first (already-created) one, before ever reaching the second.
            if 'already' in msg or '409' in msg:
                messages.append(f'{preset_name}: already exists.')
            else:
                app.logger.error(f'Video preset setup failed for {preset_name}: {e}')
                return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'message': ' Video uploads are ready to use. ' + ' '.join(messages)})

# ── API: Get hero media (photos + videos for the homepage slider)
@app.route('/api/hero_media', methods=['GET'])
def get_hero_media():
    items = [hero_media_to_dict(m) for m in get_hero_media_col().find().sort('created_at', 1)]
    return jsonify(items)

# ── API: Add hero media record
@app.route('/api/hero_media', methods=['POST'])
def add_hero_media():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data = request.json
    url  = data.get('url', '')
    if not url:
        return jsonify({'success': False, 'error': 'Missing url'}), 400
    item = {
        'type':       data.get('type', 'photo'),
        'url':        url,
        'caption':    data.get('caption', ''),
        'created_at': datetime.datetime.now().isoformat()
    }
    result = get_hero_media_col().insert_one(item)
    item['id'] = str(result.inserted_id)
    item.pop('_id', None)
    return jsonify({'success': True, 'item': item})

# ── API: Delete hero media
@app.route('/api/hero_media/<media_id>', methods=['DELETE'])
def delete_hero_media(media_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    get_hero_media_col().delete_one({'_id': ObjectId(media_id)})
    return jsonify({'success': True})

# ── Ads — short promo videos for reposting on social media + the Android
# app's own "Ads" tab. Deliberately NOT products: just a video and a
# caption, no name/price. Never shown on the website itself — nothing in
# templates/ renders these. Two read routes, same separation-of-contract
# reasoning as /api/products vs /api/catalog: the admin dashboard needs to
# see every ad (including hidden ones) to manage them, the app must only
# ever see the ones marked available.
@app.route('/api/ads', methods=['GET'])
def api_ads():
    ads = [ad_to_dict(a) for a in get_ads_col().find({'available': True}).sort('created_at', -1)]
    return jsonify(ads)

@app.route('/api/admin/ads', methods=['GET'])
def admin_list_ads():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    ads = [ad_to_dict(a) for a in get_ads_col().find().sort('created_at', -1)]
    return jsonify(ads)

@app.route('/api/ads', methods=['POST'])
def add_ad():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data = request.json or {}
    video_url = data.get('video_url', '').strip()
    if not video_url:
        return jsonify({'success': False, 'error': 'No video URL provided'}), 400
    ad = {
        'video_url':  video_url,
        'caption':    data.get('caption', '').strip(),
        'available':  True,
        'created_at': datetime.datetime.now().isoformat()
    }
    result = get_ads_col().insert_one(ad)
    ad['id'] = str(result.inserted_id)
    ad.pop('_id', None)
    return jsonify({'success': True, 'ad': ad})

@app.route('/api/ads/<ad_id>', methods=['PUT'])
def update_ad(ad_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data = request.json or {}
    data.pop('id', None)
    get_ads_col().update_one({'_id': ObjectId(ad_id)}, {'$set': data})
    return jsonify({'success': True})

@app.route('/api/ads/<ad_id>', methods=['DELETE'])
def delete_ad(ad_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    get_ads_col().delete_one({'_id': ObjectId(ad_id)})
    return jsonify({'success': True})

# ── API: Get customer reviews
@app.route('/api/reviews', methods=['GET'])
def get_reviews():
    items = [review_to_dict(r) for r in get_reviews_col().find().sort('created_at', 1)]
    return jsonify(items)

# ── API: Add customer review (from admin dashboard — published immediately)
@app.route('/api/reviews', methods=['POST'])
def add_review():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data     = request.json
    name     = data.get('name', '').strip()
    feedback = data.get('feedback', '').strip()
    if not name or not feedback:
        return jsonify({'success': False, 'error': 'Name and feedback are required'}), 400
    item = {
        'name':       name,
        'feedback':   feedback,
        'image':      data.get('image', ''),
        'status':     'approved',
        'source':     'admin',
        'created_at': datetime.datetime.now().isoformat()
    }
    result = get_reviews_col().insert_one(item)
    item['id'] = str(result.inserted_id)
    item.pop('_id', None)
    return jsonify({'success': True, 'review': item})

# ── API: Submit a review from the public site — held for admin approval
@app.route('/api/reviews/public', methods=['POST'])
def add_public_review():
    data     = request.json
    name     = data.get('name', '').strip()[:100]
    feedback = data.get('feedback', '').strip()[:1000]
    if not name or not feedback:
        return jsonify({'success': False, 'error': 'Name and feedback are required'}), 400
    item = {
        'name':       name,
        'feedback':   feedback,
        'image':      '',
        'status':     'pending',
        'source':     'customer',
        'created_at': datetime.datetime.now().isoformat()
    }
    get_reviews_col().insert_one(item)
    return jsonify({'success': True})

# ── API: Approve a pending customer review
@app.route('/api/reviews/<review_id>/approve', methods=['POST'])
def approve_review(review_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    get_reviews_col().update_one({'_id': ObjectId(review_id)}, {'$set': {'status': 'approved'}})
    return jsonify({'success': True})

# ── API: Delete customer review
@app.route('/api/reviews/<review_id>', methods=['DELETE'])
def delete_review(review_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    get_reviews_col().delete_one({'_id': ObjectId(review_id)})
    return jsonify({'success': True})

# ── API: Add announcement (admin)
@app.route('/api/announcements', methods=['POST'])
def add_announcement():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data  = request.json
    title = data.get('title', '').strip()
    body  = data.get('body', '').strip()
    if not title or not body:
        return jsonify({'success': False, 'error': 'Title and body are required'}), 400
    item = {
        'title':      title,
        'body':       body,
        'link':       data.get('link', '').strip(),
        'type':       data.get('type', 'update'),
        'active':     bool(data.get('active', True)),
        'created_at': datetime.datetime.utcnow()
    }
    result = get_announcements_col().insert_one(item)
    item['id'] = str(result.inserted_id)
    item.pop('_id', None)
    item['created_at'] = item['created_at'].isoformat()
    return jsonify({'success': True, 'announcement': item})

# ── API: Update announcement (admin) — edit fields and/or toggle active
@app.route('/api/announcements/<announcement_id>', methods=['PUT'])
def update_announcement(announcement_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data = request.json
    data.pop('id', None)
    data.pop('created_at', None)
    get_announcements_col().update_one({'_id': ObjectId(announcement_id)}, {'$set': data})
    return jsonify({'success': True})

# ── API: Delete announcement (admin)
@app.route('/api/announcements/<announcement_id>', methods=['DELETE'])
def delete_announcement(announcement_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    get_announcements_col().delete_one({'_id': ObjectId(announcement_id)})
    return jsonify({'success': True})

# Announcements are typed by hand in the admin dashboard, so the link field
# sometimes arrives messy — a leading list marker ("1.https://...") or a bare
# domain with no scheme. Cleaned once here, at the single source both the
# desktop app's in-tool popup and the LiteAgent toast pull from, so neither
# consumer needs its own copy of this logic.
def clean_announcement_link(url):
    if not url:
        return ''
    url = url.strip()
    m = re.search(r'https?://\S+', url)
    if m:
        return m.group(0)
    if re.match(r'^[\w.-]+\.\w{2,}(/\S*)?$', url):
        return 'https://' + url
    return url

# ── API: Public — current active announcement, polled by the LaptopSeal desktop app.
# "version" is the announcement's own id — the desktop app remembers the last id it
# showed and only pops up a new notification when this id changes.
@app.route('/api/laptopseal/notification', methods=['GET'])
def laptopseal_notification():
    a = get_announcements_col().find_one({'active': True}, sort=[('created_at', -1)])
    if not a:
        return jsonify({})
    return jsonify({
        'version': str(a['_id']),
        'title':   a.get('title', ''),
        'body':    a.get('body', ''),
        'link':    clean_announcement_link(a.get('link', '')),
        'type':    a.get('type', 'update')
    })

# ── API: Email subscribe
@app.route('/api/subscribe', methods=['POST'])
def subscribe():
    data  = request.json
    email = data.get('email', '').strip()
    if not email or '@' not in email:
        return jsonify({'success': False}), 400
    if not get_emails_col().find_one({'email': email}):
        get_emails_col().insert_one({'email': email, 'created_at': datetime.datetime.now().isoformat()})
    return jsonify({'success': True})

# ── API: AI chat (Light widget) — server-side so the Anthropic key never reaches the browser
@app.route('/api/ai-chat', methods=['POST'])
def ai_chat():
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'AI is not configured'}), 503

    data = request.get_json(force=True, silent=True) or {}
    messages = data.get('messages', [])
    if not isinstance(messages, list) or not messages:
        return jsonify({'error': 'No messages provided'}), 400

    # Keep the payload sane regardless of what the client sends
    messages = messages[-20:]

    try:
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'Content-Type':      'application/json'
            },
            json={
                'model':      'claude-sonnet-4-20250514',
                'max_tokens': 300,
                'system':     build_ai_system_prompt(),
                'messages':   messages
            },
            timeout=20
        )
        result = r.json()
        if r.status_code != 200:
            app.logger.error(f'Anthropic API error: {result}')
            return jsonify({'error': 'AI request failed'}), 502
        reply = result['content'][0]['text']
        return jsonify({'reply': reply})
    except Exception as e:
        app.logger.error(f'AI chat failed: {e}')
        return jsonify({'error': 'AI request failed'}), 500

# ── API: Send bulk email via Brevo
@app.route('/api/send_bulk_email', methods=['POST'])
def send_bulk_email():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401

    data       = request.json
    subject    = data.get('subject', '').strip()
    message    = data.get('message', '').strip()
    targets    = data.get('targets', 'all')
    email_mode = data.get('mode', 'branded')

    if not subject or not message:
        return jsonify({'success': False, 'error': 'Subject and message are required'}), 400

    if targets == 'all':
        recipients = [e['email'] for e in get_emails_col().find()]
    elif isinstance(targets, dict) and targets.get('mode') == 'both':
        sub_emails = [e['email'] for e in get_emails_col().find()]
        extra      = targets.get('extra', [])
        recipients = list(set(sub_emails + extra))
    elif isinstance(targets, list):
        recipients = targets
    else:
        recipients = [e['email'] for e in get_emails_col().find()]

    if not recipients:
        return jsonify({'success': False, 'error': 'No recipients found. Add subscribers or paste emails above.'}), 400

    safe_msg = message.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    if email_mode == 'plain':
        html_body = (
            '<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.8;color:#000;max-width:600px;">'
            + safe_msg.replace('\n', '<br>')
            + '<br><br>--<br>Victor<br>Light Ideas Technology<br>WhatsApp: +234 816 944 1990'
            + '</div>'
        )
        text_body = message + '\n\n--\nVictor\nLight Ideas Technology\nWhatsApp: +234 816 944 1990'
    else:
        html_body = (
            '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#000;color:#fff;">'
            '<div style="background:#000;padding:20px;text-align:center;border-bottom:3px solid #FFD700;">'
            '<h1 style="color:#FFD700;font-size:22px;margin:0;">LIGHT IDEAS TECHNOLOGY</h1>'
            '<p style="color:#888;font-size:11px;margin:4px 0 0;">Tested and Confirmed</p>'
            '</div>'
            '<div style="padding:24px;background:#111;">'
            '<pre style="color:#e8e8e8;font-size:14px;line-height:1.7;white-space:pre-wrap;font-family:Arial,sans-serif;">' + safe_msg + '</pre>'
            '</div>'
            '<div style="background:#000;padding:16px;text-align:center;">'
            '<a href="https://wa.me/2348169441990" style="background:#25D366;color:#fff;padding:8px 20px;border-radius:50px;text-decoration:none;font-weight:bold;font-size:12px;display:inline-block;margin:4px;">WhatsApp Victor</a>'
            '<a href="https://lightideastechnology.com.ng/catalog" style="background:#FFD700;color:#000;padding:8px 20px;border-radius:50px;text-decoration:none;font-weight:bold;font-size:12px;display:inline-block;margin:4px;">Browse Catalog</a>'
            '<p style="color:#444;font-size:10px;margin-top:12px;">Light Ideas Technology - Lagos, Nigeria. Reply STOP to unsubscribe.</p>'
            '</div></div>'
        )
        text_body = message + '\n\n---\nLight Ideas Technology\nWhatsApp: +234 816 944 1990\nReply STOP to unsubscribe'

    def send_via_brevo(recips, subj, html, text):
        for recip in recips:
            try:
                send_brevo_email(recip, subj, html, text)
            except Exception:
                pass

    t = threading.Thread(target=send_via_brevo, args=(recipients, subject, html_body, text_body))
    t.daemon = True
    t.start()

    return jsonify({
        'success': True,
        'sent':    len(recipients),
        'failed':  0,
        'total':   len(recipients),
        'message': f'Sending to {len(recipients)} recipient(s) via Brevo. Check inbox in 1-2 minutes.'
    })

# ── Landing page
@app.route('/links')
def links_page():
    return render_template('links.html')

# ── Ad landing page (single-purpose, for Facebook ads)
@app.route('/join')
def join_page():
    return render_template('join.html')

# ── API: Track link clicks
@app.route('/api/track_click', methods=['POST'])
def track_click():
    data = request.json
    link = data.get('link', '')
    source = data.get('source', 'unknown')
    if link:
        get_db()['link_clicks'].update_one(
            {'link': link},
            {'$inc': {'count': 1}, '$set': {'last_clicked': datetime.datetime.now().isoformat()}},
            upsert=True
        )
        # Also log each click with its source for per-source analytics
        get_db()['click_log'].insert_one({
            'link': link,
            'source': source,
            'ts': datetime.datetime.now().isoformat()
        })
    return jsonify({'success': True})

@app.route('/api/link_stats')
def link_stats():
    if not session.get('admin_logged_in'):
        return jsonify([]), 401
    stats = list(get_db()['link_clicks'].find({}, {'_id': 0}))
    return jsonify(stats)

# ── API: Per-source click analytics (for dashboard)
@app.route('/api/click_analytics')
def click_analytics():
    if not session.get('admin_logged_in'):
        return jsonify({}), 401
    logs = list(get_db()['click_log'].find({}, {'_id': 0}))
    # Breakdown by source
    by_source = {}
    by_link_source = {}
    by_day = {}
    for l in logs:
        src = l.get('source', 'unknown')
        link = l.get('link', 'unknown')
        by_source[src] = by_source.get(src, 0) + 1
        key = link + '|' + src
        by_link_source[key] = by_link_source.get(key, 0) + 1
        day = (l.get('ts', '') or '')[:10]
        if day:
            by_day[day] = by_day.get(day, 0) + 1
    return jsonify({
        'total': len(logs),
        'by_source': by_source,
        'by_link_source': by_link_source,
        'by_day': by_day
    })

# ─── LAPTOPSEAL ROUTES ─────────────────────────────────────────────────────

# ─── LAPTOPCARE ROUTES ─────────────────────────────────────────────────────

@app.route('/laptopcare')
def laptopcare():
    return render_template(
        'laptopcare.html',
        paystack_public_key=os.environ.get('PAYSTACK_PUBLIC_KEY', ''),
        paystack_plan_code=os.environ.get('PAYSTACK_LAPTOPCARE_PLAN_CODE', ''),
        price=LAPTOPCARE_PRICE_NAIRA
    )

# ── API: Verify a LaptopCare subscription payment, record the subscriber, and
# log them straight in (the password they set on the payment form is what
# they'll use to sign back in later on the member dashboard).
@app.route('/laptopcare/verify_payment', methods=['POST'])
def laptopcare_verify_payment():
    data      = request.get_json(silent=True) or {}
    reference = data.get('reference')
    name      = data.get('name', '').strip()
    phone     = data.get('phone', '').strip()
    email     = data.get('email', '').strip()
    password  = data.get('password', '')
    if not reference or not name or not phone or not email or not password:
        return jsonify({'success': False, 'error': 'Missing required details'}), 400

    headers = {'Authorization': 'Bearer ' + os.environ.get('PAYSTACK_SECRET_KEY', '')}
    r = requests.get('https://api.paystack.co/transaction/verify/' + reference, headers=headers)
    res = r.json()
    if not (res.get('status') and res['data']['status'] == 'success'):
        return jsonify({'success': False, 'error': 'Payment verification failed'})

    pdata = res['data']
    customer_code = (pdata.get('customer') or {}).get('customer_code', '')

    # The transaction-verify response doesn't carry the subscription code
    # Paystack auto-created for a plan charge — fetch it separately so we can
    # ask Paystack to stop future billing later if this member cancels.
    subscription_code = ''
    if customer_code:
        try:
            sub_r = requests.get(
                'https://api.paystack.co/subscription',
                headers=headers, params={'customer': customer_code}, timeout=10
            )
            sub_list = (sub_r.json().get('data') or [])
            if sub_list:
                subscription_code = sub_list[0].get('subscription_code', '')
        except Exception:
            pass  # not fatal — cancellation just falls back to a manual Paystack step

    now = datetime.datetime.utcnow()
    result = get_laptopcare_col().insert_one({
        'name':            name,
        'phone':           phone,
        'email':           email,
        'password_hash':   generate_password_hash(password),
        'reference':       reference,
        'customer_code':   customer_code,
        'plan_code':       pdata.get('plan', ''),
        'subscription_code': subscription_code,
        'status':          'active',
        'source':          'paystack',
        'start_date':      now,
        'entitlements':    {k: {'used': 0, 'period_start': now} for k in LAPTOPCARE_SERVICES}
    })
    session['laptopcare_member_id'] = str(result.inserted_id)
    return jsonify({'success': True})

# ── API: Manually add a LaptopCare subscriber (admin only) — for customers who
# pay by bank transfer instead of card, where Paystack recurring isn't used.
# A random PIN is generated as their sign-in password since there's no
# checkout form to collect one — hand it to the customer over WhatsApp.
@app.route('/api/laptopcare/manual', methods=['POST'])
def laptopcare_add_manual():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data  = request.json
    name  = data.get('name', '').strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    if not name or not phone:
        return jsonify({'success': False, 'error': 'Name and phone are required'}), 400
    pin = '%06d' % secrets.randbelow(1000000)
    now = datetime.datetime.utcnow()
    get_laptopcare_col().insert_one({
        'name':            name,
        'phone':           phone,
        'email':           email,
        'password_hash':   generate_password_hash(pin),
        'reference':       '',
        'customer_code':   '',
        'plan_code':       '',
        'subscription_code': '',
        'status':          'active',
        'source':          'manual',
        'start_date':      now,
        'entitlements':    {k: {'used': 0, 'period_start': now} for k in LAPTOPCARE_SERVICES}
    })
    return jsonify({'success': True, 'pin': pin})

# ── LaptopCare member sign-in (email set at checkout + the password chosen then,
# or the PIN Victor shares for manually-added subscribers)
@app.route('/laptopcare/login', methods=['GET', 'POST'])
def laptopcare_login():
    if request.method == 'GET':
        return render_template('laptopcare_login.html')
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    sub = get_laptopcare_col().find_one({'email': {'$regex': '^' + re.escape(email) + '$', '$options': 'i'}})
    if not sub or not sub.get('password_hash') or not check_password_hash(sub['password_hash'], password):
        return jsonify({'success': False, 'error': 'Incorrect email or password'}), 401
    session['laptopcare_member_id'] = str(sub['_id'])
    return jsonify({'success': True})

@app.route('/laptopcare/logout')
def laptopcare_logout():
    session.pop('laptopcare_member_id', None)
    return redirect(url_for('laptopcare_login'))

# ── LaptopCare member dashboard — status, dates, and this period's entitlements
@app.route('/laptopcare/dashboard')
def laptopcare_dashboard():
    member_id = session.get('laptopcare_member_id')
    if not member_id:
        return redirect(url_for('laptopcare_login'))
    sub = get_laptopcare_col().find_one({'_id': ObjectId(member_id)})
    if not sub:
        session.pop('laptopcare_member_id', None)
        return redirect(url_for('laptopcare_login'))
    if sub.get('status') == 'active':
        sub = laptopcare_reset_if_needed(sub)
    services = []
    entitlements = sub.get('entitlements', {})
    for key, info in LAPTOPCARE_SERVICES.items():
        services.append({
            'label': info['label'],
            'used': (entitlements.get(key) or {}).get('used', 0),
            'limit': info['limit']
        })
    return render_template(
        'laptopcare_dashboard.html',
        sub=laptopcare_to_dict(sub),
        services=services,
        price=LAPTOPCARE_PRICE_NAIRA
    )

# ── API: Cancel my membership — the dashboard shows one retention message
# client-side first; this endpoint only runs once the member confirms anyway.
@app.route('/laptopcare/cancel', methods=['POST'])
def laptopcare_cancel():
    member_id = session.get('laptopcare_member_id')
    if not member_id:
        return jsonify({'success': False}), 401
    sub = get_laptopcare_col().find_one({'_id': ObjectId(member_id)})
    if not sub:
        return jsonify({'success': False}), 404

    # Best-effort: ask Paystack to stop future billing. Requires the
    # subscription's email_token, which Paystack only hands over via webhook —
    # not implemented yet, so this currently only ever no-ops safely. Until
    # that's added, Victor must also disable the subscription by hand in the
    # Paystack dashboard whenever a member cancels.
    if sub.get('subscription_code') and sub.get('email_token'):
        try:
            headers = {'Authorization': 'Bearer ' + os.environ.get('PAYSTACK_SECRET_KEY', '')}
            requests.post(
                'https://api.paystack.co/subscription/disable',
                headers=headers,
                json={'code': sub['subscription_code'], 'token': sub['email_token']},
                timeout=10
            )
        except Exception:
            pass

    get_laptopcare_col().update_one(
        {'_id': sub['_id']},
        {'$set': {'status': 'cancelled', 'cancelled_at': datetime.datetime.utcnow()}}
    )
    return jsonify({'success': True})

# ── API: Admin marks one of a member's included services as used this period
@app.route('/api/laptopcare/<sub_id>/mark_used', methods=['POST'])
def laptopcare_mark_used(sub_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data = request.get_json(silent=True) or {}
    service = data.get('service', '')
    if service not in LAPTOPCARE_SERVICES:
        return jsonify({'success': False, 'error': 'Unknown service'}), 400
    sub = get_laptopcare_col().find_one({'_id': ObjectId(sub_id)})
    if not sub:
        return jsonify({'success': False, 'error': 'Subscriber not found'}), 404
    sub = laptopcare_reset_if_needed(sub)
    used = (sub.get('entitlements', {}).get(service) or {}).get('used', 0)
    limit = LAPTOPCARE_SERVICES[service]['limit']
    if used >= limit:
        return jsonify({'success': False, 'error': 'Already used ' + str(limit) + ' this period'}), 400
    get_laptopcare_col().update_one(
        {'_id': sub['_id']},
        {'$set': {'entitlements.' + service + '.used': used + 1}}
    )
    return jsonify({'success': True, 'used': used + 1, 'limit': limit})

@app.route('/laptopseal')
def laptopseal():
    # Checkout is explicitly disabled until a sandbox/production release is
    # approved.  The browser never contains a fallback live key.
    return render_template('laptopseal.html',
                           paystack_public_key=os.environ.get('PAYSTACK_PUBLIC_KEY', ''),
                           payments_enabled=os.environ.get('LAPTOPSEAL_PAYMENTS_ENABLED') == '1')

@app.route('/laptopseal/tool')
def laptopseal_tool():
    # LaptopSeal is now a desktop app only. The old browser tool is retired —
    # send anyone who lands here back to the sales / download page.
    return redirect('/laptopseal')


def _vendor_payment_configuration_error():
    """Vendor checkout has its own release gate; legacy checkout is unchanged."""
    public = os.environ.get('PAYSTACK_PUBLIC_KEY', '')
    secret = os.environ.get('PAYSTACK_SECRET_KEY', '')
    if os.environ.get('LAPTOPSEAL_TEST_MODE') == '1':
        if (configured_mongo_db_name() != 'laptopseal_test'
                or not public.startswith('pk_test_')
                or not secret.startswith('sk_test_')):
            return 'Isolated vendor payment configuration is unavailable.'
    elif (configured_mongo_db_name() != PRODUCTION_MONGO_DB_NAME
          or not public.startswith('pk_live_')
          or not secret.startswith('sk_live_')):
        return 'Vendor payment configuration is unavailable.'
    if not app.secret_key:
        return 'Payment session is unavailable.'
    return None


def _same_origin_vendor_purchase():
    if request.headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
        return False
    origin = request.headers.get('Origin')
    if not origin:
        return True
    parsed = urlsplit(origin)
    return (parsed.netloc == request.host
            and (parsed.scheme == 'https'
                 or (parsed.scheme == 'http'
                     and os.environ.get('LAPTOPSEAL_TEST_MODE') == '1'
                     and request.host.split(':')[0] in ('127.0.0.1', 'localhost'))))


def _verify_paystack_reference(reference):
    """Shared direct Paystack verification; a redirect or webhook is no proof."""
    if not safe_reference(reference):
        raise ValueError('Invalid payment reference.')
    secret = os.environ.get('PAYSTACK_SECRET_KEY', '')
    if not secret:
        raise RuntimeError('Paystack is not configured.')
    response = requests.get('https://api.paystack.co/transaction/verify/' + reference,
                            headers={'Authorization': 'Bearer ' + secret}, timeout=15)
    response.raise_for_status()
    verification = response.json()
    return verification.get('data') if verification.get('status') else None


def _send_vendor_package_email(package, email):
    """A purchase notice, never a license key. Test mode cannot send mail."""
    if os.environ.get('LAPTOPSEAL_TEST_MODE') == '1' or not BREVO_API_KEY:
        return False
    name = package['plan_id'].capitalize()
    price = '₦' + format(package['amount_kobo'] // 100, ',')
    purchased = package['issued_at'].strftime('%d %B %Y %H:%M UTC')
    expiry = package['expires_at'].strftime('%d %B %Y %H:%M UTC')
    portal = 'https://lightideastechnology.com.ng/laptopseal/vendor'
    steps = ('Install LaptopSeal on the laptop.', 'Open LaptopSeal.',
             'Select Activate with QR Code.', 'Open the portal link on your phone.',
             'Verify your email when requested.', 'Scan the laptop QR code.',
             'Approve activation.')
    text = (f'Your LaptopSeal {name} package supports {package["max_devices"]} laptops.\n'
            f'Price paid: {price}\nPurchase date: {purchased}\nExpires: {expiry}\n'
            f'Purchase reference: {package["purchase_reference"]}\n'
            f'Verify your email at {portal}. Keep this link for future activations.\n\n'
            + '\n'.join(f'{number}. {step}' for number, step in enumerate(steps, 1)))
    html = ('<p>Your LaptopSeal ' + html_escape(name) + ' package supports '
            + str(package['max_devices']) + ' laptops.</p><p>Price paid: '
            + html_escape(price) + '<br>Purchase date: ' + html_escape(purchased)
            + '<br>Expires: ' + html_escape(expiry) + '<br>Purchase reference: '
            + html_escape(package['purchase_reference']) + '</p><p><a href="' + portal
            + '">Open the vendor portal on your phone</a>. Keep this link for future activations; email verification may be required again.</p><ol>'
            + ''.join('<li>' + html_escape(step) + '</li>' for step in steps) + '</ol>')
    try:
        return bool(send_brevo_email(email, 'Your LaptopSeal vendor package', html, text))
    except Exception:
        return False


def _deliver_vendor_package_email(db, package, email):
    """Claim a pending notice once; failure leaves the issued package intact."""
    collection = db.ls_vendor_packages
    if os.environ.get('LAPTOPSEAL_TEST_MODE') == '1':
        collection.update_one({'_id': package['_id'], 'delivery_status': 'pending'},
                              {'$set': {'delivery_status': 'skipped_test'}})
        return
    claimed = collection.find_one_and_update(
        {'_id': package['_id'], 'delivery_status': 'pending'},
        {'$set': {'delivery_status': 'sending'}, '$inc': {'delivery_attempts': 1}},
        return_document=ReturnDocument.AFTER)
    if not claimed:
        return
    sent = _send_vendor_package_email(claimed, email)
    collection.update_one({'_id': package['_id'], 'delivery_status': 'sending'},
                          {'$set': {'delivery_status': 'sent' if sent else 'pending'}})


def _fulfill_verified_vendor_payment(intent, transaction):
    db = get_db()
    try:
        package, created = issue_paid_vendor_package(db, intent, transaction)
    except VendorLicenseError:
        return {'success': False, 'error': 'Payment details could not be verified.', 'status': 400}
    except Exception:
        return {'success': False, 'error': 'Package issuance is unavailable. Retry verification.', 'status': 503}
    try:
        _deliver_vendor_package_email(db, package, intent['email'])
    except Exception:
        pass  # The issued package remains available through verified-email login.
    return {'success': True, 'already_fulfilled': not created,
            'plan_id': package['plan_id'], 'max_devices': package['max_devices'],
            'expires_at': package['expires_at'].isoformat(),
            'confirmation_path': '/laptopseal/vendor/confirmation'}


@app.route('/laptopseal/vendor/initialize_payment', methods=['POST'])
def vendor_initialize_payment():
    if os.environ.get('LAPTOPSEAL_VENDOR_PAYMENTS_ENABLED') != '1':
        return jsonify({'success': False, 'error': 'Vendor checkout is not enabled.'}), 403
    error = _vendor_payment_configuration_error()
    if error:
        return jsonify({'success': False, 'error': error}), 503
    if not request.is_json or not _same_origin_vendor_purchase():
        return jsonify({'success': False, 'error': 'Invalid purchase request.'}), 403
    data = request.get_json(silent=True) or {}
    email = normalize_vendor_email(data.get('email'))
    plan_id = data.get('plan_id')
    if not email or plan_id not in VENDOR_PLANS:
        return jsonify({'success': False, 'error': 'Choose a package and enter a valid email.'}), 400
    try:
        require_vendor_payment_indexes(get_db())
    except Exception:
        return jsonify({'success': False, 'error': 'Vendor checkout setup is incomplete.'}), 503
    reference = 'LSV_' + secrets.token_urlsafe(24).replace('-', '').replace('_', '')
    plan = VENDOR_PLANS[plan_id]
    get_ls_payment_intents_col().insert_one({
        'reference': reference, 'product_id': VENDOR_PRODUCT_ID,
        'email': email, 'plan_id': plan_id, 'amount': plan['amount_kobo'],
        'currency': 'NGN', 'environment': ('test' if os.environ.get('LAPTOPSEAL_TEST_MODE') == '1'
                                         else 'production'),
        'state': 'pending', 'created_at': datetime.datetime.utcnow(),
    })
    session['laptopseal_vendor_payment_reference'] = reference
    return jsonify({'success': True, 'reference': reference,
                    'amount': plan['amount_kobo'], 'currency': 'NGN'})


@app.route('/laptopseal/vendor/verify_payment', methods=['POST'])
def vendor_verify_payment():
    if os.environ.get('LAPTOPSEAL_VENDOR_PAYMENTS_ENABLED') != '1':
        return jsonify({'success': False, 'error': 'Vendor checkout is not enabled.'}), 403
    error = _vendor_payment_configuration_error()
    if error:
        return jsonify({'success': False, 'error': error}), 503
    if not request.is_json or not _same_origin_vendor_purchase():
        return jsonify({'success': False, 'error': 'Invalid payment request.'}), 403
    reference = str((request.get_json(silent=True) or {}).get('reference') or '').strip()
    if (not safe_reference(reference)
            or session.get('laptopseal_vendor_payment_reference') != reference):
        return jsonify({'success': False, 'error': 'Start payment again from the vendor page.'}), 400
    intent = get_ls_payment_intents_col().find_one({'reference': reference,
                                                    'product_id': VENDOR_PRODUCT_ID})
    if not intent:
        return jsonify({'success': False, 'error': 'Purchase is unavailable.'}), 404
    if intent.get('state') == 'fulfilled':
        package = get_db().ls_vendor_packages.find_one({
            'purchase_reference': reference, '_id': intent.get('package_id')})
        if package:
            try:
                _deliver_vendor_package_email(get_db(), package, intent['email'])
            except Exception:
                pass
            return jsonify({'success': True, 'already_fulfilled': True,
                            'confirmation_path': '/laptopseal/vendor/confirmation'})
        return jsonify({'success': False, 'error': 'Payment fulfilment state is inconsistent.'}), 409
    try:
        transaction = _verify_paystack_reference(reference)
    except Exception:
        return jsonify({'success': False, 'error': 'Payment verification is unavailable. Retry later.'}), 503
    outcome = _fulfill_verified_vendor_payment(intent, transaction)
    return jsonify(outcome), (200 if outcome['success'] else outcome['status'])

@app.route('/laptopseal/verify_payment', methods=['POST'])
def laptopseal_verify_payment():
    if os.environ.get('LAPTOPSEAL_PAYMENTS_ENABLED') != '1':
        return jsonify({'success': False, 'error': 'LaptopSeal checkout is not enabled.'}), 403
    configuration_error = laptopseal_test_database_error()
    if configuration_error:
        return jsonify({'success': False, 'error': configuration_error}), 503
    data = request.get_json(silent=True) or {}
    reference = str(data.get('reference') or '').strip()
    intent = get_ls_payment_intents_col().find_one({'reference': reference})
    if (not safe_reference(reference) or not intent
            or session.get('laptopseal_payment_reference') != reference):
        return jsonify({'success': False, 'error': 'Start payment again from the LaptopSeal page.'}), 400
    if intent.get('state') == 'fulfilled':
        # The same browser can submit its callback twice.  A fulfilled intent
        # with its already-created licence is a successful no-op, not a second
        # Paystack fulfilment or delivery attempt.
        if get_db()['ls_licenses'].find_one({'reference': reference, 'active': True}):
            session['laptopseal_delivery_reference'] = reference
            return jsonify({'success': True, 'already_fulfilled': True,
                            'message': 'Payment was already confirmed. Continue to your secure delivery page.'})
        return jsonify({'success': False, 'error': 'Payment fulfilment state is inconsistent.'}), 409
    if intent.get('state') != 'pending':
        return jsonify({'success': False, 'error': 'Payment is not available for verification.'}), 409
    email = intent['email']
    plan_id = intent['plan_id']
    if not email or plan_id not in PLANS:
        return jsonify({'success': False, 'error': 'Invalid payment details.'}), 400
    if not os.environ.get('PAYSTACK_SECRET_KEY') or not LAPTOPSEAL_LICENSE_PEPPER:
        return jsonify({'success': False, 'error': 'Checkout is not configured.'}), 503
    try:
        transaction = _verify_paystack_reference(reference)
    except Exception:
        return jsonify({'success': False, 'error': 'Payment verification is unavailable.'}), 503
    verified_plan = verified_plan_from_transaction(transaction, plan_id, email)
    if not verified_plan:
        return jsonify({'success': False, 'error': 'Payment details could not be verified.'}), 400
    outcome = _fulfill_verified_laptopseal_payment(transaction, verified_plan, email)
    if outcome.get('success') or outcome.get('already_fulfilled'):
        session['laptopseal_delivery_reference'] = reference
    status = 200 if outcome['success'] else outcome.get('status', 400)
    return jsonify(outcome), status

@app.route('/laptopseal/initialize_payment', methods=['POST'])
def laptopseal_initialize_payment():
    if os.environ.get('LAPTOPSEAL_PAYMENTS_ENABLED') != '1':
        return jsonify({'success': False, 'error': 'LaptopSeal checkout is not enabled.'}), 403
    configuration_error = laptopseal_test_database_error()
    if configuration_error:
        return jsonify({'success': False, 'error': configuration_error}), 503
    data = request.get_json(silent=True) or {}
    email, plan_id = normalise_email(data.get('email')), str(data.get('plan_id') or '').strip()
    if not email or plan_id not in PLANS or not os.environ.get('PAYSTACK_PUBLIC_KEY'):
        return jsonify({'success': False, 'error': 'Invalid purchase details.'}), 400
    try:
        _delivery_cipher()
    except Exception:
        return jsonify({'success': False, 'error': 'Secure license delivery is unavailable.'}), 503
    reference = 'LS_' + secrets.token_urlsafe(24).replace('-', '').replace('_', '')
    plan = PLANS[plan_id]
    get_ls_payment_intents_col().insert_one({'reference': reference, 'email': email,
        'plan_id': plan_id, 'amount': plan['amount'], 'currency': 'NGN', 'state': 'pending',
        'created_at': datetime.datetime.utcnow()})
    session['laptopseal_payment_reference'] = reference
    return jsonify({'success': True, 'reference': reference, 'amount': plan['amount'], 'currency': 'NGN'})

def _fulfill_verified_laptopseal_payment(transaction, plan_id, email):
    """Fulfil once.  A unique reference index is the final concurrency guard."""
    reference = str(transaction.get('reference') or '').strip()
    if not safe_reference(reference):
        return {'success': False, 'error': 'Invalid verified payment reference.', 'status': 400}
    plan = PLANS[plan_id]
    token = make_token('LS')
    try:
        encrypted_token = _encrypt_delivery_token(token)
    except Exception:
        return {'success': False, 'error': 'Secure license delivery is unavailable.', 'status': 503}
    now = datetime.datetime.utcnow()
    document = {
        'email': normalise_email(email), 'token_hash': _license_hash(token),
        'token_prefix': token_prefix(token), 'reference': reference,
        'amount': plan['amount'], 'currency': 'NGN', 'tier': plan_id,
        'max_devices': plan['max_devices'], 'devices': [], 'active': True,
        'created_at': now, 'expires_at': now + datetime.timedelta(days=30),
        'hardware_id': None, 'fulfilment_source': 'paystack_verified',
        'delivery_token_encrypted': encrypted_token, 'delivery_status': 'pending', 'delivery_attempts': 0
    }
    try:
        get_db()['ls_licenses'].insert_one(document)
    except DuplicateKeyError:
        # Never reveal a prior licence secret to an arbitrary replay caller.
        return {'success': True, 'already_fulfilled': True,
                'message': 'Payment was already confirmed. Continue to your secure delivery page.'}
    stored = get_db()['ls_licenses'].find_one({'reference': reference})
    # Write an issuance audit only after the unique licence insert succeeds.
    # Duplicate payment callbacks return above, so they cannot create another
    # licence, audit record, or delivery attempt.
    get_ls_license_audit_col().insert_one({
        'event': 'customer_license_issued', 'license_id': str(stored['_id']),
        'reference': reference, 'token_prefix': stored.get('token_prefix', ''),
        'tier': plan_id, 'amount': plan['amount'], 'currency': 'NGN',
        'max_devices': plan['max_devices'], 'created_at': now,
        'actor': 'paystack_verified'
    })
    delivered = _record_delivery_attempt(stored, token)
    get_ls_payment_intents_col().update_one({'reference': reference}, {'$set': {'state': 'fulfilled'}})
    return {'success': True, 'tier': plan_id, 'max_devices': plan['max_devices'],
            'expires_at': document['expires_at'].isoformat(), 'email_delivered': delivered}

@app.route('/webhooks/paystack/laptopseal', methods=['POST'])
def laptopseal_paystack_webhook():
    raw_body = request.get_data(cache=True)
    signature = request.headers.get('x-paystack-signature', '')
    if not paystack_signature_is_valid(raw_body, signature, PAYSTACK_WEBHOOK_SECRET):
        return jsonify({'success': False, 'error': 'Invalid webhook signature.'}), 401
    payload = request.get_json(silent=True) or {}
    event = payload.get('event')
    transaction = payload.get('data') or {}
    reference = str(transaction.get('reference') or '')
    event_key = str(payload.get('id') or (event or '') + ':' + reference)
    if event != 'charge.success' or not safe_reference(reference):
        return jsonify({'success': True, 'ignored': True})
    intent = get_ls_payment_intents_col().find_one({'reference': reference})
    if intent and intent.get('product_id') == VENDOR_PRODUCT_ID:
        error = _vendor_payment_configuration_error()
        if error:
            return jsonify({'success': False, 'error': error}), 503
        try:
            verified = _verify_paystack_reference(reference)
        except Exception:
            return jsonify({'success': False, 'error': 'Payment verification is unavailable.'}), 503
        outcome = _fulfill_verified_vendor_payment(intent, verified)
        if outcome['success']:
            try:
                get_ls_paystack_events_col().insert_one({
                    'event_key': event_key, 'received_at': datetime.datetime.utcnow()})
            except DuplicateKeyError:
                pass
        return jsonify({'success': outcome['success'],
                        'duplicate': outcome.get('already_fulfilled', False)}), (
                            200 if outcome['success'] else outcome['status'])
    metadata = transaction.get('metadata') or {}
    email = normalise_email((transaction.get('customer') or {}).get('email'))
    plan_id = metadata.get('plan_id')
    if plan_id not in PLANS:
        # A checkout page already open before deployment carries the former
        # descriptive metadata.  Infer only the exact known plan label.
        legacy_product = str(metadata.get('product') or '')
        plan_id = next((candidate for candidate, plan in PLANS.items()
                        if legacy_product.startswith('LaptopSeal')
                        and legacy_product.endswith(plan['label'])), None)
    if verified_plan_from_transaction(transaction, plan_id, email) is None:
        return jsonify({'success': False, 'error': 'Webhook payment does not match LaptopSeal checkout.'}), 400
    outcome = _fulfill_verified_laptopseal_payment(transaction, plan_id, email)
    try:
        get_ls_paystack_events_col().insert_one({'event_key': event_key, 'received_at': datetime.datetime.utcnow()})
    except DuplicateKeyError:
        pass
    # A browser callback may already have fulfilled this reference.  That is a
    # successful, idempotent webhook outcome rather than a failed payment.
    return jsonify({'success': bool(outcome.get('success') or outcome.get('already_fulfilled')),
                    'duplicate': bool(outcome.get('already_fulfilled'))})

@app.route('/laptopseal/activate')
def laptopseal_activate():
    reference = session.get('laptopseal_delivery_reference')
    if not reference:
        return redirect('/laptopseal')
    license_doc = get_db()['ls_licenses'].find_one({'reference': reference, 'active': True})
    if not license_doc:
        return redirect('/laptopseal')
    try:
        token = _decrypt_delivery_token(license_doc['delivery_token_encrypted'])
    except (KeyError, InvalidToken, ValueError):
        return redirect('/laptopseal')
    return render_template('laptopseal_activate.html', token=token,
                           email_delivered=license_doc.get('delivery_status') == 'sent')

@app.route('/laptopseal/recover-license', methods=['POST'])
def laptopseal_recover_license():
    """Re-send an existing encrypted key; never creates or returns another key."""
    data = request.get_json(silent=True) or {}
    reference, email = str(data.get('reference') or '').strip(), normalise_email(data.get('email'))
    response = {'success': True, 'message': 'If the purchase details match, we will email your activation key.'}
    if not safe_reference(reference) or not email:
        return jsonify(response)
    license_doc = get_db()['ls_licenses'].find_one({'reference': reference, 'email': email, 'active': True})
    if not license_doc:
        return jsonify(response)
    try:
        token = _decrypt_delivery_token(license_doc['delivery_token_encrypted'])
    except (KeyError, InvalidToken, ValueError):
        return jsonify(response)
    _record_delivery_attempt(license_doc, token)
    return jsonify(response)

@app.route('/laptopseal/bind_hardware', methods=['POST'])
def laptopseal_bind_hardware(token=None, hardware_id=None, generic_errors=False):
    if token is None or hardware_id is None:
        data = request.get_json(silent=True) or {}
        token = str(data.get('token') or '').strip()
        hardware_id = str(data.get('hardware_id') or '').strip()
    def fail(message, status=None):
        response = jsonify({'success': False,
                            'error': 'Activation could not be completed.' if generic_errors else message})
        return (response, status) if status else response
    if not token or not hardware_id:
        return fail('Missing data')
    lic = _find_laptopseal_license(token)
    if not lic:
        return fail('Invalid license')
    if lic.get('expires_at') and lic['expires_at'] <= datetime.datetime.utcnow():
        return fail('License expired')

    # Backward compatibility: migrate old single-hardware_id licenses to a device list
    devices = lic.get('devices')
    if devices is None:
        devices = [lic['hardware_id']] if lic.get('hardware_id') else []
    max_devices = lic.get('max_devices', 1)

    # Already registered on this laptop → fine
    if hardware_id in devices:
        return jsonify({'success': True, 'message': 'License verified',
                        'tier': lic.get('tier', 'single'),
                        'devices_used': len(devices), 'max_devices': max_devices})

    # Room for another laptop?
    if len(devices) < max_devices:
        devices.append(hardware_id)
        # The query includes the prior list size so concurrent requests cannot
        # silently overrun a purchased device limit.
        write = get_db()['ls_licenses'].update_one({
            '_id': lic['_id'],
            '$or': [{'devices': devices[:-1]}, {'devices': {'$exists': False}}]
        },
            {'$set': {'devices': devices, 'max_devices': max_devices,
                      'hardware_id': devices[0]}})
        if not write.modified_count:
            return fail('Activation changed; please retry.', 409)
        return jsonify({'success': True, 'message': 'License activated on this device',
                        'tier': lic.get('tier', 'single'),
                        'devices_used': len(devices), 'max_devices': max_devices})

    # Limit reached
    return jsonify({'success': False,
                    'error': ('Activation could not be completed.' if generic_errors else
                              'This license has reached its limit of ' + str(max_devices) + ' laptop(s).'),
                    'devices_used': len(devices), 'max_devices': max_devices})

@app.route('/laptopseal/check_license', methods=['POST'])
def laptopseal_check_license():
    data = request.get_json(silent=True) or {}
    token = str(data.get('token') or '').strip()
    hardware_id = str(data.get('hardware_id') or '').strip()
    if not token:
        return jsonify({'valid': False, 'tier': 'free'})
    lic = _find_laptopseal_license(token)
    if not lic:
        return jsonify({'valid': False, 'tier': 'free'})
    now = datetime.datetime.utcnow()
    if lic.get('expires_at') and lic['expires_at'] <= now:
        get_db()['ls_licenses'].update_one({'_id': lic['_id']}, {'$set': {'active': False}})
        return jsonify({'valid': False, 'tier': 'free', 'error': 'License expired'})

    # Backward compatibility: migrate old single-hardware_id licenses
    devices = lic.get('devices')
    if devices is None:
        devices = [lic['hardware_id']] if lic.get('hardware_id') else []
    max_devices = lic.get('max_devices', 1)

    # This laptop must be one of the registered devices
    if not hardware_id or hardware_id not in devices:
        return jsonify({'valid': False, 'tier': 'free', 'error': 'Not activated on this device',
                        'devices_used': len(devices), 'max_devices': max_devices})

    expires_at = lic['expires_at']
    days_left = (expires_at - now).days
    return jsonify({
        'valid': True,
        'tier': lic.get('tier', 'pro'),
        'email': lic['email'],
        'days_left': days_left,
        'expires_at': expires_at.strftime('%d %B %Y'),
        # The legacy display value above is retained for compatibility.  The
        # desktop uses these exact UTC values to enforce bounded offline access.
        'expires_at_utc': expires_at.replace(tzinfo=datetime.timezone.utc).isoformat(),
        'server_time_utc': now.replace(tzinfo=datetime.timezone.utc).isoformat(),
        'devices_used': len(devices),
        'max_devices': max_devices
    })

# ── API: Public — the desktop app calls this on launch (and periodically) so
# Victor can see which devices are running LaptopSeal, not just who bought a
# license.  Activation secrets are intentionally never accepted by telemetry.
@app.route('/api/laptopseal/checkin', methods=['POST'])
def laptopseal_checkin():
    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id')
    if not device_id:
        return jsonify({'success': False, 'error': 'Missing device_id'}), 400
    now = datetime.datetime.utcnow()

    set_fields = {
        'device_name':  data.get('device_name', ''),
        'device_model': data.get('device_model', ''),
        'app_version':  data.get('app_version', ''),
        'last_seen':    now
    }

    # This telemetry endpoint deliberately does not receive activation secrets.
    # Ownership is recorded only by the authenticated activation flow.

    get_ls_installs_col().update_one(
        {'device_id': device_id},
        {
            '$set': set_fields,
            '$setOnInsert': {'first_seen': now}
        },
        upsert=True
    )
    return jsonify({'success': True})

# ── API: Public — a laptop calls this once it has actually shown an
# announcement (toast or in-tool popup), so the admin dashboard can report
# "Reached X of Y laptops". Upserted on (announcement_id, device_id), so a
# laptop that re-shows the same announcement (e.g. LiteAgent + the in-tool
# popup both firing) is only ever counted once.
@app.route('/api/laptopseal/ack', methods=['POST'])
def laptopseal_ack():
    data = request.get_json(silent=True) or {}
    device_id = data.get('device_id')
    version = str(data.get('version', '')).strip()
    if not device_id or not version:
        return jsonify({'success': False, 'error': 'Missing device_id or version'}), 400
    get_ls_acks_col().update_one(
        {'announcement_id': version, 'device_id': device_id},
        {'$set': {'acked_at': datetime.datetime.utcnow()}},
        upsert=True
    )
    return jsonify({'success': True})

# ── API: Admin — how many laptops have acked each announcement, polled by the
# dashboard's Announcements tab so "Reached X of Y" updates live without a
# manual page reload.
@app.route('/api/announcements/reach_counts')
def announcements_reach_counts():
    if not session.get('admin_logged_in'):
        return jsonify({}), 401
    total_installs = get_ls_installs_col().count_documents({})
    counts = {}
    for a in get_announcements_col().find({}, {'_id': 1}):
        aid = str(a['_id'])
        counts[aid] = {
            'reached': get_ls_acks_col().count_documents({'announcement_id': aid}),
            'total_installs': total_installs
        }
    return jsonify(counts)

# ── API: LaptopSeal installs list (admin) — fetched by the dashboard's LaptopSeal tab
@app.route('/api/laptopseal/installs')
def laptopseal_installs():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    raw = list(get_ls_installs_col().find().sort('last_seen', -1))
    installs = []
    for i in raw:
        i = install_to_dict(i)
        installs.append(i)
    return jsonify({'installs': installs, 'total': len(installs)})

@app.route('/laptopseal/admin_data')
def laptopseal_admin_data():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    now = datetime.datetime.utcnow()
    raw = list(get_db()['ls_licenses'].find())
    # Newest first (sort on the real datetime before formatting)
    raw.sort(key=lambda l: l.get('created_at') or datetime.datetime.min, reverse=True)

    licenses = []
    active = 0
    expired = 0
    total_revenue = 0
    for l in raw:
        amount = l.get('amount', 0)
        total_revenue += amount
        exp = l.get('expires_at')
        is_active_flag = bool(l.get('active'))
        days_left = None
        status = 'expired'
        if exp:
            days_left = (exp - now).days
            if is_active_flag and exp > now:
                status = 'active'
            else:
                status = 'expired'
        elif is_active_flag:
            status = 'active'

        if status == 'active':
            active += 1
        else:
            expired += 1

        # Device usage (with backward compatibility)
        devices = l.get('devices')
        if devices is None:
            devices = [l['hardware_id']] if l.get('hardware_id') else []
        max_devices = l.get('max_devices', 1)
        tier = l.get('tier', 'single')

        licenses.append({
            'license_id':  str(l.get('_id', '')),
            'email':       l.get('email', '—'),
            # Customer bearer tokens are never returned to an administrator UI.
            'token_prefix': l.get('token_prefix', ''),
            'reference':   l.get('reference', ''),
            'amount':      amount,
            'tier':        tier,
            'devices_used': len(devices),
            'max_devices': max_devices,
            'hardware_id': (devices[0] if devices else ''),
            'activated':   len(devices) > 0,
            'created_at':  l['created_at'].strftime('%d %b %Y') if l.get('created_at') else '—',
            'expires_at':  exp.strftime('%d %b %Y') if exp else '—',
            'days_left':   days_left if days_left is not None else '—',
            'status':      status
        })

    return jsonify({
        'licenses': licenses,
        'total':    len(licenses),
        'active':   active,
        'expired':  expired,
        'revenue':  total_revenue / 100
    })

def _owner_admin_authenticated(data):
    supplied = data.get('admin_password', '')
    return (bool(session.get('admin_logged_in')) and bool(ADMIN_PASSWORD_HASH)
            and isinstance(supplied, str) and bool(supplied)
            and check_password_hash(ADMIN_PASSWORD_HASH, supplied))

def _admin_json_mutation_authenticated(data):
    """Require the existing admin re-authentication on a same-origin JSON action.

    The endpoint has no CORS grant.  Reject an explicit cross-site browser
    request as defence in depth; a cross-site form cannot submit this JSON body
    or the re-authentication password.  Non-browser admin API clients remain
    supported when they supply the session and password.
    """
    if not request.is_json:
        return False
    if request.headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
        return False
    return _owner_admin_authenticated(data)

@app.route('/api/laptopseal/licenses/revoke', methods=['POST'])
def revoke_laptopseal_license():
    """Revoke one customer license without receiving or returning its token."""
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    if not _admin_json_mutation_authenticated(data):
        return jsonify({'success': False, 'error': 'Administrator re-authentication failed.'}), 403

    reference = str(data.get('reference') or '').strip()
    if not safe_reference(reference):
        return jsonify({'success': False, 'error': 'A valid payment reference is required.'}), 400
    license_doc = get_db()['ls_licenses'].find_one({'reference': reference})
    if not license_doc:
        return jsonify({'success': False, 'error': 'License not found.'}), 404

    now = datetime.datetime.utcnow()
    result = get_db()['ls_licenses'].update_one(
        {'_id': license_doc['_id'], 'active': True},
        {'$set': {'active': False, 'revoked_at': now, 'revocation_reason': 'administrator_revoked'}})
    if not result.modified_count:
        return jsonify({'success': False, 'error': 'License is already inactive.'}), 409

    # Do not store an administrator password or a customer bearer token in the
    # audit record.  The payment reference and token prefix identify the event
    # for support without granting activation access.
    get_ls_license_audit_col().insert_one({
        'event': 'customer_license_revoked', 'license_id': str(license_doc['_id']),
        'reference': reference, 'token_prefix': license_doc.get('token_prefix', ''),
        'created_at': now, 'actor': 'authenticated_admin'
    })
    return jsonify({'success': True, 'message': 'License revoked.'})

@app.route('/api/laptopseal/owner-master-key', methods=['GET', 'POST'])
def owner_master_key_admin():
    """Set or rotate one stable owner password. Never return its plaintext/hash."""
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    if request.method == 'GET':
        key = get_ls_owner_key_col().find_one({'_id': 'primary'}, {'password_hash': 0}) or {}
        return jsonify({'configured': bool(key.get('active')),
                        'changed_at': key['changed_at'].isoformat() if key.get('changed_at') else None})
    data = request.get_json(silent=True) or {}
    if not _owner_admin_authenticated(data):
        return jsonify({'success': False, 'error': 'Owner re-authentication failed.'}), 403
    master_key = data.get('master_key')
    if not valid_master_key(master_key):
        return jsonify({'success': False, 'error': 'Owner key must be 20–256 characters without leading or trailing spaces.'}), 400
    current = get_ls_owner_key_col().find_one({'_id': 'primary', 'active': True})
    if current and verify_master_key(master_key, current.get('password_hash')):
        return jsonify({'success': True, 'message': 'Owner key is unchanged.'})
    now = datetime.datetime.utcnow()
    get_ls_owner_key_col().update_one(
        {'_id': 'primary'},
        {'$set': {'password_hash': hash_master_key(master_key), 'active': True,
                  'changed_at': now}, '$inc': {'version': 1}}, upsert=True)
    return jsonify({'success': True, 'message': 'Owner key set. Existing owner device sessions must reactivate.'})

@app.route('/api/laptopseal/owner-master-key/revoke', methods=['POST'])
def revoke_owner_master_key():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    if not _owner_admin_authenticated(data):
        return jsonify({'success': False, 'error': 'Owner re-authentication failed.'}), 403
    result = get_ls_owner_key_col().update_one(
        {'_id': 'primary', 'active': True},
        {'$set': {'active': False, 'revoked_at': datetime.datetime.utcnow()},
         '$inc': {'version': 1}})
    return jsonify({'success': bool(result.modified_count)})

def _owner_attempt_allowed(hardware_id):
    """Limit failed guesses, not legitimate activations, across IP/device/global buckets."""
    now = datetime.datetime.utcnow()
    window = int(now.timestamp()) // OWNER_RATE_WINDOW_SECONDS
    source = request.remote_addr or 'unknown'
    for scope, value, limit in (('ip', source, OWNER_RATE_MAX_ATTEMPTS),
                                ('device', hardware_id, OWNER_RATE_MAX_ATTEMPTS),
                                ('global', 'all', OWNER_RATE_MAX_ATTEMPTS * 20)):
        bucket_id = scope + ':' + str(window) + ':' + opaque_id(value, LAPTOPSEAL_LICENSE_PEPPER)
        attempts = get_ls_owner_attempts_col().find_one({'_id': bucket_id}) or {}
        if attempts.get('count', 0) >= limit:
            return False
    return True


def _record_owner_failed_attempt(hardware_id):
    """Atomic increments after a failed key; never stores the submitted key."""
    now = datetime.datetime.utcnow()
    window = int(now.timestamp()) // OWNER_RATE_WINDOW_SECONDS
    source = request.remote_addr or 'unknown'
    for scope, value in (('ip', source), ('device', hardware_id), ('global', 'all')):
        bucket_id = scope + ':' + str(window) + ':' + opaque_id(value, LAPTOPSEAL_LICENSE_PEPPER)
        get_ls_owner_attempts_col().find_one_and_update(
            {'_id': bucket_id},
            {'$inc': {'count': 1},
             '$setOnInsert': {'expires_at': now + datetime.timedelta(hours=1)}},
            upsert=True, return_document=ReturnDocument.AFTER)

@app.route('/laptopseal/owner/activate', methods=['POST'])
def laptopseal_owner_activate(submitted_key=None, hardware_id=None, generic_errors=False):
    if submitted_key is None or hardware_id is None:
        data = request.get_json(silent=True) or {}
        hardware_id = str(data.get('hardware_id') or '').strip()
        submitted_key = data.get('master_key')
    def fail(message, status):
        return jsonify({'success': False,
                        'error': 'Activation could not be completed.' if generic_errors else message}), status
    if not LAPTOPSEAL_LICENSE_PEPPER:
        return fail('Owner activation is unavailable.', 503)
    if not hardware_id or len(hardware_id) > 256 or not isinstance(submitted_key, str):
        return fail('Invalid activation request.', 400)
    if not _owner_attempt_allowed(hardware_id):
        return fail('Too many attempts. Try again later.', 429)
    config = get_ls_owner_key_col().find_one({'_id': 'primary', 'active': True})
    if not config or not verify_master_key(submitted_key, config.get('password_hash')):
        _record_owner_failed_attempt(hardware_id)
        return fail('Invalid owner key.', 403)
    now = datetime.datetime.utcnow()
    expires_at = now + datetime.timedelta(days=OWNER_ACCESS_DAYS)
    device_token = new_device_token()
    device_id = opaque_id(hardware_id, LAPTOPSEAL_LICENSE_PEPPER)
    get_ls_owner_activations_col().update_one(
        {'_id': device_id},
        {'$set': {'hardware_id': hardware_id,
                  'token_hash': device_token_hash(device_token, LAPTOPSEAL_LICENSE_PEPPER),
                  'key_version': config['version'], 'activated_at': now,
                  'expires_at': expires_at}}, upsert=True)
    return jsonify({'success': True, 'owner_token': device_token,
                    'days_left': OWNER_ACCESS_DAYS, 'expires_at': expires_at.isoformat()})

@app.route('/laptopseal/activate_key', methods=['POST'])
def laptopseal_activate_key():
    """Unified activation without exposing or trusting a client key type."""
    data = request.get_json(silent=True) or {}
    key = str(data.get('key') or '').strip()
    hardware_id = str(data.get('hardware_id') or '').strip()
    if not key or not hardware_id or len(hardware_id) > 256:
        return jsonify({'success': False, 'error': 'Activation could not be completed.'}), 400
    if _is_known_laptopseal_key(key):
        return laptopseal_bind_hardware(key, hardware_id, generic_errors=True)
    return laptopseal_owner_activate(key, hardware_id, generic_errors=True)

@app.route('/laptopseal/owner/check', methods=['POST'])
def laptopseal_owner_check():
    data = request.get_json(silent=True) or {}
    hardware_id = str(data.get('hardware_id') or '').strip()
    owner_token = data.get('owner_token')
    invalid = {'valid': False, 'tier': 'free'}
    if (not LAPTOPSEAL_LICENSE_PEPPER or not hardware_id or len(hardware_id) > 256
            or not isinstance(owner_token, str) or len(owner_token) > 128):
        return jsonify(invalid)
    config = get_ls_owner_key_col().find_one({'_id': 'primary', 'active': True})
    if not config:
        return jsonify(invalid)
    activation = get_ls_owner_activations_col().find_one(
        {'_id': opaque_id(hardware_id, LAPTOPSEAL_LICENSE_PEPPER)})
    if (not activation or activation.get('key_version') != config.get('version')
            or not activation.get('expires_at')
            or activation['expires_at'] <= datetime.datetime.utcnow()
            or not hmac.compare_digest(
                activation.get('token_hash', ''),
                device_token_hash(owner_token, LAPTOPSEAL_LICENSE_PEPPER))):
        return jsonify(invalid)
    remaining = activation['expires_at'] - datetime.datetime.utcnow()
    days_left = max(1, (remaining.days + (remaining.seconds > 0)))
    return jsonify({'valid': True, 'tier': 'owner', 'days_left': days_left,
                    'expires_at': activation['expires_at'].isoformat()})

@app.route('/victor-admin/laptopseal')
def laptopseal_admin_page():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    return render_template('laptopseal_admin.html')


@app.route('/laptopseal/apps.json')
def laptopseal_apps():
    # Curated app list for the LaptopSeal App Store. Edit a link here and every
    # laptop gets the fix instantly — no app rebuild needed.
    apps = [
        {"id":"chrome","icon":"&#127760;","name":"Google Chrome","cat":"browser","desc":"Fast and secure web browser","size":"~90MB","url":"https://dl.google.com/tag/s/dl/chrome/install/googlechromestandaloneenterprise64.msi","installer":"GoogleChrome.msi","silent":"/quiet"},
        {"id":"firefox","icon":"&#129418;","name":"Mozilla Firefox","cat":"browser","desc":"Free and open source browser","size":"~55MB","url":"https://download.mozilla.org/?product=firefox-latest-ssl&os=win64&lang=en-US","installer":"FirefoxSetup.exe","silent":"/S"},
        {"id":"brave","icon":"&#129409;","name":"Brave Browser","cat":"browser","desc":"Privacy browser with ad blocking","size":"~90MB","url":"https://laptop-updates.brave.com/latest/winx64","installer":"BraveSetup.exe","silent":"--silent"},
        {"id":"opera","icon":"&#128308;","name":"Opera Browser","cat":"browser","desc":"Feature-rich browser with free VPN","size":"~80MB","url":"https://net.geo.opera.com/opera/stable/windows","installer":"OperaSetup.exe","silent":"/silent /launchopera 0"},
        {"id":"vlc","icon":"&#127916;","name":"VLC Media Player","cat":"media","desc":"Plays all video and audio formats","size":"~40MB","url":"https://get.videolan.org/vlc/3.0.21/win64/vlc-3.0.21-win64.exe","installer":"vlc-setup.exe","silent":"/S"},
        {"id":"spotify","icon":"&#127925;","name":"Spotify","cat":"media","desc":"Music streaming app","size":"~75MB","url":"https://download.scdn.co/SpotifySetup.exe","installer":"SpotifySetup.exe","silent":"/silent"},
        {"id":"libreoffice","icon":"&#128196;","name":"LibreOffice","cat":"productivity","desc":"Free office suite - Writer, Calc, Impress","size":"~350MB","url":"https://mirror.ufs.ac.za/tdf/libreoffice/stable/24.8.5/win/x86_64/LibreOffice_24.8.5_Win_x86-64.msi","installer":"LibreOffice.msi","silent":"/quiet"},
        {"id":"foxit","icon":"&#128203;","name":"Foxit PDF Reader","cat":"productivity","desc":"Lightweight PDF reader","size":"~60MB","url":"https://cdn07.foxitsoftware.com/pub/foxit/reader/desktop/win/2.x/2.4/en_us/FoxitReader251_Setup_Prom_IS.exe","installer":"FoxitReader.exe","silent":"/quiet"},
        {"id":"notepadpp","icon":"&#128221;","name":"Notepad++","cat":"productivity","desc":"Advanced text and code editor","size":"~4MB","url":"https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.6.7/npp.8.6.7.Installer.x64.exe","installer":"npp.installer.exe","silent":"/S"},
        {"id":"7zip","icon":"&#128476;","name":"7-Zip","cat":"utility","desc":"Free file archiver - best compression","size":"~1.5MB","url":"https://www.7-zip.org/a/7z2401-x64.exe","installer":"7z-setup.exe","silent":"/S"},
        {"id":"winrar","icon":"&#128230;","name":"WinRAR","cat":"utility","desc":"File archiver and extractor","size":"~3MB","url":"https://www.win-rar.com/fileadmin/winrar-versions/winrar/winrar-x64-701.exe","installer":"winrar-setup.exe","silent":"/S"},
        {"id":"ccleaner","icon":"&#129529;","name":"CCleaner Free","cat":"utility","desc":"PC cleaner and optimizer","size":"~25MB","url":"https://download.ccleaner.com/ccsetup.exe","installer":"ccsetup.exe","silent":"/S"},
        {"id":"cpu-z","icon":"&#9889;","name":"CPU-Z","cat":"utility","desc":"Detailed CPU and hardware info","size":"~2MB","url":"https://download.cpuid.com/cpu-z/cpu-z_2.09-en.exe","installer":"cpuz.exe","silent":"/S"},
        {"id":"hwmonitor","icon":"&#127777;","name":"HWMonitor","cat":"utility","desc":"Hardware temperature monitor","size":"~2MB","url":"https://download.cpuid.com/hwmonitor/hwmonitor_1.53.exe","installer":"hwmonitor.exe","silent":"/S"},
        {"id":"teamviewer","icon":"&#128421;","name":"TeamViewer","cat":"utility","desc":"Remote desktop and support tool","size":"~50MB","url":"https://download.teamviewer.com/download/TeamViewer_Setup_x64.exe","installer":"TeamViewerSetup.exe","silent":"/S"},
        {"id":"anydesk","icon":"&#128279;","name":"AnyDesk","cat":"utility","desc":"Fast remote desktop application","size":"~4MB","url":"https://download.anydesk.com/AnyDesk.exe","installer":"AnyDesk.exe","silent":"--install C:\\AnyDesk --start-with-win --silent"},
        {"id":"zoom","icon":"&#128249;","name":"Zoom","cat":"communication","desc":"Video meetings and conferencing","size":"~10MB","url":"https://zoom.us/client/latest/ZoomInstaller.exe","installer":"ZoomInstaller.exe","silent":"/quiet /norestart"},
        {"id":"telegram","icon":"&#9992;","name":"Telegram Desktop","cat":"communication","desc":"Fast and secure messaging app","size":"~50MB","url":"https://telegram.org/dl/desktop/win64","installer":"tsetup-x64.exe","silent":"/VERYSILENT"},
        {"id":"discord","icon":"&#128172;","name":"Discord","cat":"communication","desc":"Voice, video and text chat","size":"~90MB","url":"https://discord.com/api/downloads/distributions/app/installers/latest?channel=stable&platform=win&arch=x64","installer":"DiscordSetup.exe","silent":"/S"}
    ]
    return jsonify({'apps': apps})


@app.route('/terms')
def terms_page():
    return render_template('terms.html')

@app.route('/laptopcare/terms')
def laptopcare_terms_page():
    return render_template('laptopcare_terms.html')


@app.route('/save')
def save_contact_page():
    return render_template('save.html')


@app.route('/save-contact.vcf')
def save_contact_vcf():
    vcard = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "N:Light Ideas Technology;;;;\r\n"
        "FN:Light Ideas Technology\r\n"
        "ORG:Light Ideas Technology\r\n"
        "TEL;TYPE=CELL:+2348169441990\r\n"
        "URL:https://lightideastechnology.com.ng\r\n"
        "NOTE:Tested & Confirmed laptops — Just for your convenience\r\n"
        "END:VCARD\r\n"
    )
    return Response(
        vcard,
        mimetype='text/vcard',
        headers={'Content-Disposition': 'attachment; filename="LightIdeasTechnology.vcf"'}
    )


@app.route('/laptopseal/userguide')
def laptopseal_userguide():
    return render_template('guide.html')


@app.route('/guide')
def guide_alias():
    return render_template('guide.html')


# ── LaptopSeal installer + auto-update (hosted permanently on GitHub Releases)
LAPTOPSEAL_LATEST_VERSION = '1.0.12'
LAPTOPSEAL_SETUP_URL = 'https://github.com/Victor441990/lightideas-website/releases/download/v1.0.12/LaptopSeal_Setup.exe'
@app.route('/laptopseal/download')
def laptopseal_download():
    user_agent = request.headers.get('User-Agent', '').lower()
    mobile_keywords = ['android', 'iphone', 'ipad', 'ipod', 'mobile', 'blackberry', 'windows phone']
    if any(kw in user_agent for kw in mobile_keywords):
        return render_template('laptopseal_mobile_notice.html')
    return redirect(LAPTOPSEAL_SETUP_URL)

@app.route('/laptopseal/version.json')
def laptopseal_version():
    return jsonify({
        'version': LAPTOPSEAL_LATEST_VERSION,
        'url':     LAPTOPSEAL_SETUP_URL,
        'notes':   'Improved Windows/Office license checking — now flags a Volume/KMS-licensed copy (a sign of a pirated install), not just whether it reports as activated. The "What Can This Laptop Run?" check now correctly recognizes newer processors (Intel Core Ultra, 12th/13th generation, AMD Ryzen AI) and checks Adobe\'s DirectX/SSE requirements.'
    })

from laptopseal_vendor_api import register_vendor_api
register_vendor_api(app, get_db, send_brevo_email)

@app.route('/laptopseal/vendor')
def laptopseal_vendor_portal():
    response = app.make_response(render_template('laptopseal_vendor.html'))
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/laptopseal/vendor/purchase')
def laptopseal_vendor_purchase():
    enabled = (os.environ.get('LAPTOPSEAL_VENDOR_PAYMENTS_ENABLED') == '1'
               and _vendor_payment_configuration_error() is None)
    response = app.make_response(render_template(
        'laptopseal_vendor_purchase.html', plans=VENDOR_PLANS,
        selected_plan=request.args.get('plan') if request.args.get('plan') in VENDOR_PLANS else 'single',
        payments_enabled=enabled,
        paystack_public_key=os.environ.get('PAYSTACK_PUBLIC_KEY', '') if enabled else ''))
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/laptopseal/vendor/confirmation')
def laptopseal_vendor_confirmation():
    reference = session.get('laptopseal_vendor_payment_reference')
    if not safe_reference(reference):
        return redirect('/laptopseal/vendor/purchase')
    intent = get_ls_payment_intents_col().find_one({
        'reference': reference, 'product_id': VENDOR_PRODUCT_ID})
    if not intent:
        return redirect('/laptopseal/vendor/purchase')
    package = get_db().ls_vendor_packages.find_one({
        'purchase_reference': reference, '_id': intent.get('package_id')})
    response = app.make_response(render_template(
        'laptopseal_vendor_confirmation.html', reference=reference,
        package=package, plan_id=intent['plan_id'], email=intent['email'],
        max_devices=VENDOR_PLANS[intent['plan_id']]['max_devices']))
    response.headers['Cache-Control'] = 'no-store'
    return response

if __name__ == '__main__':
    app.run(debug=True, port=5050)
