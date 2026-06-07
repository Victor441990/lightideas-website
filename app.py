from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient
from bson import ObjectId
import cloudinary
import cloudinary.uploader
import datetime, os, json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

app = Flask(__name__)
app.secret_key = 'lightideas_secret_2026_victor'

# ── MongoDB
MONGO_URI = "mongodb+srv://enema910_db_user:MsHwrG1k3TNCCcAr@cluster0.b0rxo4f.mongodb.net/?appName=Cluster0"
client       = MongoClient(MONGO_URI)
db           = client['lightideas']
products_col = db['products']
emails_col   = db['emails']

# ── Cloudinary
cloudinary.config(
    cloud_name = 'dfkdvznkp',
    api_key    = '519772916967931',
    api_secret = 'a-tRq7QYepp6jxEx9TFlk10OMpQ'
)

# ── Email config
EMAIL_ADDRESS  = 'lightideastechnology1@gmail.com'
EMAIL_PASSWORD = 'gqoiomeokqkyllhc'

ADMIN_PASSWORD = 'lightideas2026'

def product_to_dict(p):
    p['id'] = str(p['_id'])
    del p['_id']
    return p

# ── MAIN WEBSITE
@app.route('/')
def index():
    products = [product_to_dict(p) for p in products_col.find({'available': True})]
    return render_template('index.html', products=products)

# ── CATALOG
@app.route('/catalog')
def catalog():
    products = [product_to_dict(p) for p in products_col.find({'available': True})]
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
    products = [product_to_dict(p) for p in products_col.find()]
    emails   = [e['email'] for e in emails_col.find()]
    return render_template('admin_dashboard.html', products=products, emails=emails)

# ── ADMIN LOGOUT
@app.route('/victor-admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

# ── API: Get all products
@app.route('/api/products', methods=['GET'])
def get_products():
    products = [product_to_dict(p) for p in products_col.find({'available': True})]
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
    result = products_col.insert_one(product)
    product['id'] = str(result.inserted_id)
    return jsonify({'success': True, 'product': product})

# ── API: Update product
@app.route('/api/products/<product_id>', methods=['PUT'])
def update_product(product_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data = request.json
    data.pop('id', None)
    products_col.update_one({'_id': ObjectId(product_id)}, {'$set': data})
    return jsonify({'success': True})

# ── API: Delete product
@app.route('/api/products/<product_id>', methods=['DELETE'])
def delete_product(product_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    products_col.delete_one({'_id': ObjectId(product_id)})
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
    if not emails_col.find_one({'email': email}):
        emails_col.insert_one({'email': email, 'created_at': datetime.datetime.now().isoformat()})
    return jsonify({'success': True})

# ── API: Send bulk email
@app.route('/api/send_bulk_email', methods=['POST'])
def send_bulk_email():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False}), 401
    data    = request.json
    subject = data.get('subject', '').strip()
    message = data.get('message', '').strip()
    targets = data.get('targets', 'all')  # 'all' or list of emails

    if not subject or not message:
        return jsonify({'success': False, 'error': 'Subject and message are required'}), 400

    # Get recipients
    if targets == 'all':
        recipients = [e['email'] for e in emails_col.find()]
    elif isinstance(targets, dict) and targets.get('mode') == 'both':
        # All subscribers + extra emails
        sub_emails = [e['email'] for e in emails_col.find()]
        extra = targets.get('extra', [])
        recipients = list(set(sub_emails + extra))  # no duplicates
    elif isinstance(targets, list):
        recipients = targets
    else:
        recipients = [e['email'] for e in emails_col.find()]

    if not recipients:
        return jsonify({'success': False, 'error': 'No subscribers found'}), 400

    sent = 0
    failed = 0
    errors = []

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)

        for email in recipients:
            try:
                msg = MIMEMultipart('alternative')
                msg['Subject'] = subject
                msg['From']    = f"Light Ideas Technology <{EMAIL_ADDRESS}>"
                msg['To']      = email

                # Plain text version
                text_body = message + "\n\n---\nLight Ideas Technology\nTested and Confirmed — Just for Your Convenience\nWhatsApp: +234 816 944 1990\nWebsite: lightideas-website.onrender.com\n\nTo unsubscribe reply with STOP"

                # HTML version
                safe_message = message.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;')
                html_body = (
                    '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#000;color:#fff;">'
                    '<div style="background:#000;padding:24px;text-align:center;border-bottom:3px solid #FFD700;">'
                    '<h1 style="color:#FFD700;font-size:24px;margin:0;letter-spacing:3px;">LIGHT IDEAS TECHNOLOGY</h1>'
                    '<p style="color:#888;font-size:12px;margin:4px 0 0;">Tested and Confirmed - Just for Your Convenience</p>'
                    '</div>'
                    '<div style="padding:32px 24px;background:#111;">'
                    '<div style="color:#e8e8e8;font-size:15px;line-height:1.7;white-space:pre-wrap;">' + safe_message + '</div>'
                    '</div>'
                    '<div style="background:#000;padding:20px 24px;border-top:1px solid #1a1a1a;text-align:center;">'
                    '<a href="https://wa.me/2348169441990" style="background:#25D366;color:#fff;padding:10px 24px;border-radius:50px;text-decoration:none;font-weight:bold;font-size:13px;display:inline-block;margin-bottom:12px;">Chat Victor on WhatsApp</a>'
                    '<br/>'
                    '<a href="https://lightideas-website.onrender.com/catalog" style="background:#FFD700;color:#000;padding:10px 24px;border-radius:50px;text-decoration:none;font-weight:bold;font-size:13px;display:inline-block;">Browse Our Catalog</a>'
                    '<p style="color:#444;font-size:11px;margin-top:16px;">Light Ideas Technology - Lagos, Nigeria<br/>To unsubscribe reply STOP</p>'
                    '</div>'
                    '</div>'
                )

                msg.attach(MIMEText(text_body, 'plain'))
                msg.attach(MIMEText(html_body, 'html'))

                server.sendmail(EMAIL_ADDRESS, email, msg.as_string())
                sent += 1
            except Exception as e:
                failed += 1
                errors.append(str(e))

        server.quit()

    except Exception as e:
        return jsonify({'success': False, 'error': f'Failed to connect to Gmail: {str(e)}'}), 500

    return jsonify({
        'success': True,
        'sent':    sent,
        'failed':  failed,
        'total':   len(recipients),
        'errors':  errors[:5]
    })

if __name__ == '__main__':
    app.run(debug=True, port=5050)