from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient
from bson import ObjectId
import cloudinary
import cloudinary.uploader
import datetime, json, requests, threading, os, os

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
BREVO_API_KEY    = os.environ.get('BREVO_API_KEY', '')
BREVO_API_URL    = 'https://api.brevo.com/v3/smtp/email'
EMAIL_SENDER     = {'name': 'Light Ideas Technology', 'email': 'lightideastechnology1@gmail.com'}

ADMIN_PASSWORD = 'lightideas2026'

def product_to_dict(p):
    p['id'] = str(p['_id'])
    del p['_id']
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

    data    = request.json
    subject = data.get('subject', '').strip()
    message = data.get('message', '').strip()
    targets = data.get('targets', 'all')

    if not subject or not message:
        return jsonify({'success': False, 'error': 'Subject and message are required'}), 400

    # Build recipient list
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

    # Build email HTML
    safe_msg  = message.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
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
        '<a href="https://lightideas-website.onrender.com/catalog" style="background:#FFD700;color:#000;padding:8px 20px;border-radius:50px;text-decoration:none;font-weight:bold;font-size:12px;display:inline-block;margin:4px;">Browse Catalog</a>'
        '<p style="color:#444;font-size:10px;margin-top:12px;">Light Ideas Technology - Lagos, Nigeria. Reply STOP to unsubscribe.</p>'
        '</div></div>'
    )
    text_body = message + '\n\n---\nLight Ideas Technology\nWhatsApp: +234 816 944 1990\nReply STOP to unsubscribe'

    # Send via Brevo in background
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

if __name__ == '__main__':
    app.run(debug=True, port=5050)