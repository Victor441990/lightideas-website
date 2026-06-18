from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient
from bson import ObjectId
import cloudinary
import cloudinary.uploader
import datetime, json, requests, threading, os, uuid

app = Flask(__name__)
app.secret_key = 'lightideas_secret_2026_victor'

# ── MongoDB
MONGO_URI = "mongodb+srv://enema910_db_user:MsHwrG1k3TNCCcAr@cluster0.b0rxo4f.mongodb.net/?appName=Cluster0"

def get_db():
    client = MongoClient(MONGO_URI)
    return client['lightideas']

def get_products_col():
    return get_db()['products']

def get_emails_col():
    return get_db()['emails']

# ── Cloudinary
cloudinary.config(
    cloud_name = 'dfkdvznkp',
    api_key    = '519772916967931',
    api_secret = 'a-tRq7QYepp6jxEx9TFlk10OMpQ'
)

# ── Brevo email config
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
EMAIL_SENDER  = {'name': 'Light Ideas Technology', 'email': 'info@lightideastechnology.com.ng'}

ADMIN_PASSWORD = 'lightideas2026'

def product_to_dict(p):
    p['id'] = str(p['_id'])
    del p['_id']
    # Sanitize string fields to prevent JS syntax errors
    for key in ['name', 'specs', 'description']:
        if key in p and p[key]:
            p[key] = str(p[key]).replace("'", "\\'").replace('"', '&quot;')
    return p
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

# ── MAIN WEBSITE
@app.route('/')
def index():
    products = [product_to_dict(p) for p in get_products_col().find({'available': True})]
    return render_template('index.html', products=products)

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
    products = [product_to_dict(p) for p in get_products_col().find()]
    emails   = [e['email'] for e in get_emails_col().find()]
    return render_template('admin_dashboard.html', products=products, emails=emails)

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
        return jsonify({'success': False, 'error': str(e)}), 500

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
    token = request.args.get('token') or request.cookies.get('ls_token')
    is_pro = False
    if token:
        license_data = get_db()['ls_licenses'].find_one({'token': token, 'active': True})
        if license_data:
            if license_data.get('expires_at') and license_data['expires_at'] > datetime.datetime.utcnow():
                is_pro = True
            else:
                get_db()['ls_licenses'].update_one({'token': token}, {'$set': {'active': False}})
    return render_template('laptopseal_tool.html', is_pro=is_pro, token=token or '')

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
        amount_paid = res['data']['amount']
        if amount_paid >= 500000:
            token = str(uuid.uuid4()).replace('-', '')
            expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=30)
            get_db()['ls_licenses'].insert_one({
                'email': email,
                'token': token,
                'reference': reference,
                'amount': amount_paid,
                'active': True,
                'created_at': datetime.datetime.utcnow(),
                'expires_at': expires_at,
                'hardware_id': None
            })
            return jsonify({'success': True, 'token': token})
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
    license_data = get_db()['ls_licenses'].find_one({'token': token, 'active': True})
    if not license_data:
        return jsonify({'success': False, 'error': 'Invalid license'})
    if not license_data.get('hardware_id'):
        get_db()['ls_licenses'].update_one({'token': token}, {'$set': {'hardware_id': hardware_id}})
        return jsonify({'success': True, 'message': 'License activated on this device'})
    if license_data['hardware_id'] == hardware_id:
        return jsonify({'success': True, 'message': 'License verified'})
    return jsonify({'success': False, 'error': 'This license is bound to a different device'})

@app.route('/laptopseal/check_license', methods=['POST'])
def laptopseal_check_license():
    data = request.get_json()
    token = data.get('token')
    hardware_id = data.get('hardware_id')
    if not token:
        return jsonify({'valid': False, 'tier': 'free'})
    license_data = get_db()['ls_licenses'].find_one({'token': token, 'active': True})
    if not license_data:
        return jsonify({'valid': False, 'tier': 'free'})
    if license_data.get('expires_at') and license_data['expires_at'] < datetime.datetime.utcnow():
        get_db()['ls_licenses'].update_one({'token': token}, {'$set': {'active': False}})
        return jsonify({'valid': False, 'tier': 'free', 'error': 'License expired'})
    if license_data.get('hardware_id') and license_data['hardware_id'] != hardware_id:
        return jsonify({'valid': False, 'tier': 'free', 'error': 'Wrong device'})
    expires_at = license_data['expires_at']
    days_left = (expires_at - datetime.datetime.utcnow()).days
    return jsonify({
        'valid': True,
        'tier': 'pro',
        'email': license_data['email'],
        'days_left': days_left,
        'expires_at': expires_at.strftime('%d %B %Y')
    })

@app.route('/laptopseal/admin_data')
def laptopseal_admin_data():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    licenses = list(get_db()['ls_licenses'].find({}, {'_id': 0}))
    for l in licenses:
        if 'created_at' in l:
            l['created_at'] = l['created_at'].strftime('%d %b %Y')
        if 'expires_at' in l:
            l['expires_at'] = l['expires_at'].strftime('%d %b %Y')
    active = sum(1 for l in licenses if l.get('active'))
    total_revenue = sum(l.get('amount', 0) for l in licenses) / 100
    return jsonify({
        'licenses': licenses,
        'total': len(licenses),
        'active': active,
        'revenue': total_revenue
    })

@app.route('/laptopseal/download')
def laptopseal_download():
    return redirect('/static/downloads/LaptopSeal_Setup.exe')
if __name__ == '__main__':
    app.run(debug=True, port=5050)