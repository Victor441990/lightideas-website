from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient
from bson import ObjectId
import cloudinary
import cloudinary.uploader
import datetime, json, requests, threading, os, uuid

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY')

# ── MongoDB
MONGO_URI = os.environ.get('MONGO_URI')

def get_db():
    client = MongoClient(MONGO_URI)
    return client['lightideas']

def get_products_col():
    return get_db()['products']

def get_emails_col():
    return get_db()['emails']

def get_hero_media_col():
    return get_db()['hero_media']

def get_reviews_col():
    return get_db()['reviews']

# ── Cloudinary
cloudinary.config(
    cloud_name = 'dfkdvznkp',
    api_key    = '318817292118594',
    api_secret = os.environ.get('CLOUDINARY_API_SECRET')
)

# ── Brevo email config
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
EMAIL_SENDER  = {'name': 'Light Ideas Technology', 'email': 'info@lightideastechnology.com.ng'}

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')

# ── Anthropic (AI chat widget)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

def product_to_dict(p):
    p['id'] = str(p['_id'])
    del p['_id']
    return p

def hero_media_to_dict(m):
    m['id'] = str(m['_id'])
    del m['_id']
    return m

def review_to_dict(r):
    r['id'] = str(r['_id'])
    del r['_id']
    return r
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
    products   = [product_to_dict(p) for p in get_products_col().find({'available': True})]
    hero_media = [hero_media_to_dict(m) for m in get_hero_media_col().find().sort('created_at', 1)]
    reviews    = [review_to_dict(r) for r in get_reviews_col().find().sort('created_at', 1)]
    hero_photos = [m for m in hero_media if m['type'] == 'photo']
    hero_videos = [m for m in hero_media if m['type'] == 'video']
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
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            error = 'Wrong password. Try again.'
    return render_template('admin_login.html', error=error)

# ── ADMIN DASHBOARD
@app.route('/victor-admin/dashboard')
def admin_dashboard():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    products   = [product_to_dict(p) for p in get_products_col().find()]
    emails     = [e['email'] for e in get_emails_col().find()]
    hero_media = [hero_media_to_dict(m) for m in get_hero_media_col().find().sort('created_at', -1)]
    reviews    = [review_to_dict(r) for r in get_reviews_col().find().sort('created_at', -1)]
    return render_template('admin_dashboard.html', products=products, emails=emails, hero_media=hero_media, reviews=reviews)

# ── ADMIN LOGOUT
@app.route('/victor-admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

# ── API: Get all products
@app.route('/api/products', methods=['GET'])
def get_products():
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
        result = cloudinary.uploader.upload(
            file,
            folder='lightideas-hero',
            resource_type='video' if media_type == 'video' else 'image'
        )
        return jsonify({'success': True, 'url': result['secure_url']})
    except Exception as e:
        app.logger.error(f'Hero media upload failed: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500

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

# ── API: Get customer reviews
@app.route('/api/reviews', methods=['GET'])
def get_reviews():
    items = [review_to_dict(r) for r in get_reviews_col().find().sort('created_at', 1)]
    return jsonify(items)

# ── API: Add customer review
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
        'created_at': datetime.datetime.now().isoformat()
    }
    result = get_reviews_col().insert_one(item)
    item['id'] = str(result.inserted_id)
    item.pop('_id', None)
    return jsonify({'success': True, 'review': item})

# ── API: Delete customer review
@app.route('/api/reviews/<review_id>', methods=['DELETE'])
def delete_review(review_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    get_reviews_col().delete_one({'_id': ObjectId(review_id)})
    return jsonify({'success': True})

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

@app.route('/laptopseal')
def laptopseal():
    return render_template('laptopseal.html', paystack_public_key=os.environ.get('PAYSTACK_PUBLIC_KEY', ''))

@app.route('/laptopseal/tool')
def laptopseal_tool():
    # LaptopSeal is now a desktop app only. The old browser tool is retired —
    # send anyone who lands here back to the sales / download page.
    return redirect('/laptopseal')

@app.route('/laptopseal/verify_payment', methods=['POST'])
def laptopseal_verify_payment():
    data = request.get_json()
    reference = data.get('reference')
    email = data.get('email')
    if not reference or not email:
        return jsonify({'success': False, 'error': 'Missing reference or email'})
    headers = {'Authorization': 'Bearer ' + os.environ.get('PAYSTACK_SECRET_KEY', '')}
    r = requests.get('https://api.paystack.co/transaction/verify/' + reference, headers=headers)
    res = r.json()
    if res.get('status') and res['data']['status'] == 'success':
        amount_paid = res['data']['amount']  # in kobo
        # Determine tier + laptop limit from the amount paid.
        if amount_paid >= 6000000:      # ₦60,000
            tier, max_devices = 'unlimited', 999999
        elif amount_paid >= 3000000:    # ₦30,000
            tier, max_devices = 'medium', 20
        elif amount_paid >= 800000:     # ₦8,000
            tier, max_devices = 'small', 5
        elif amount_paid >= 200000:     # ₦2,000
            tier, max_devices = 'single', 1
        else:
            return jsonify({'success': False, 'error': 'Amount too low for any plan'})

        token = str(uuid.uuid4()).replace('-', '')
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=30)
        get_db()['ls_licenses'].insert_one({
            'email': email,
            'token': token,
            'reference': reference,
            'amount': amount_paid,
            'tier': tier,
            'max_devices': max_devices,
            'devices': [],
            'active': True,
            'created_at': datetime.datetime.utcnow(),
            'expires_at': expires_at,
            'hardware_id': None  # kept for backward compatibility
        })
        return jsonify({'success': True, 'token': token, 'tier': tier, 'max_devices': max_devices})
    return jsonify({'success': False, 'error': 'Payment verification failed'})

@app.route('/laptopseal/activate')
def laptopseal_activate():
    token = request.args.get('token')
    email = request.args.get('email', '')
    if not token:
        return redirect('/laptopseal')
    license_data = get_db()['ls_licenses'].find_one({'token': token, 'active': True})
    if not license_data:
        return redirect('/laptopseal')
    return render_template('laptopseal_activate.html', token=token, email=email)

@app.route('/laptopseal/bind_hardware', methods=['POST'])
def laptopseal_bind_hardware():
    data = request.get_json()
    token = data.get('token')
    hardware_id = data.get('hardware_id')
    if not token or not hardware_id:
        return jsonify({'success': False, 'error': 'Missing data'})
    lic = get_db()['ls_licenses'].find_one({'token': token, 'active': True})
    if not lic:
        return jsonify({'success': False, 'error': 'Invalid license'})

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
        get_db()['ls_licenses'].update_one({'token': token},
            {'$set': {'devices': devices, 'max_devices': max_devices,
                      'hardware_id': devices[0]}})
        return jsonify({'success': True, 'message': 'License activated on this device',
                        'tier': lic.get('tier', 'single'),
                        'devices_used': len(devices), 'max_devices': max_devices})

    # Limit reached
    return jsonify({'success': False,
                    'error': 'This license has reached its limit of ' + str(max_devices) + ' laptop(s).',
                    'devices_used': len(devices), 'max_devices': max_devices})

@app.route('/laptopseal/check_license', methods=['POST'])
def laptopseal_check_license():
    data = request.get_json()
    token = data.get('token')
    hardware_id = data.get('hardware_id')
    if not token:
        return jsonify({'valid': False, 'tier': 'free'})
    lic = get_db()['ls_licenses'].find_one({'token': token, 'active': True})
    if not lic:
        return jsonify({'valid': False, 'tier': 'free'})
    if lic.get('expires_at') and lic['expires_at'] < datetime.datetime.utcnow():
        get_db()['ls_licenses'].update_one({'token': token}, {'$set': {'active': False}})
        return jsonify({'valid': False, 'tier': 'free', 'error': 'License expired'})

    # Backward compatibility: migrate old single-hardware_id licenses
    devices = lic.get('devices')
    if devices is None:
        devices = [lic['hardware_id']] if lic.get('hardware_id') else []
    max_devices = lic.get('max_devices', 1)

    # This laptop must be one of the registered devices
    if hardware_id and hardware_id not in devices:
        return jsonify({'valid': False, 'tier': 'free', 'error': 'Not activated on this device',
                        'devices_used': len(devices), 'max_devices': max_devices})

    expires_at = lic['expires_at']
    days_left = (expires_at - datetime.datetime.utcnow()).days
    return jsonify({
        'valid': True,
        'tier': lic.get('tier', 'pro'),
        'email': lic['email'],
        'days_left': days_left,
        'expires_at': expires_at.strftime('%d %B %Y'),
        'devices_used': len(devices),
        'max_devices': max_devices
    })

@app.route('/laptopseal/admin_data')
def laptopseal_admin_data():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    now = datetime.datetime.utcnow()
    raw = list(get_db()['ls_licenses'].find({}, {'_id': 0}))
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
            'email':       l.get('email', '—'),
            'token':       l.get('token', ''),
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


@app.route('/laptopseal/userguide')
def laptopseal_userguide():
    return render_template('guide.html')


@app.route('/guide')
def guide_alias():
    return render_template('guide.html')


# ── LaptopSeal installer + auto-update (hosted permanently on GitHub Releases)
LAPTOPSEAL_LATEST_VERSION = '1.0.7'
LAPTOPSEAL_SETUP_URL = 'https://github.com/Victor441990/lightideas-website/releases/download/v1.0.7/LaptopSeal_Setup.exe'
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
        'notes':   'LibreHardwareMonitor fallback for Temperature module, headphone jack detection fix, background install for antivirus/App Store apps, and a fix for the auto-updater install race condition.'
    })

if __name__ == '__main__':
    app.run(debug=True, port=5050)