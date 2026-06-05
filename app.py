from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import json, os, datetime, base64

app = Flask(__name__)
app.secret_key = 'lightideas_secret_2026_victor'

ADMIN_PASSWORD = 'lightideas2026'
PRODUCTS_FILE = 'products.json'
EMAILS_FILE   = 'emails.json'

def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return []
    with open(PRODUCTS_FILE, 'r') as f:
        return json.load(f)

def save_products(products):
    with open(PRODUCTS_FILE, 'w') as f:
        json.dump(products, f, indent=2)

def load_emails():
    if not os.path.exists(EMAILS_FILE):
        return []
    with open(EMAILS_FILE, 'r') as f:
        return json.load(f)

def save_emails(emails):
    with open(EMAILS_FILE, 'w') as f:
        json.dump(emails, f, indent=2)

def is_logged_in():
    return session.get('admin_logged_in') == True

@app.route('/')
def index():
    products = [p for p in load_products() if p.get('available', True)]
    return render_template('index.html', products=products)

@app.route('/victor-admin', methods=['GET', 'POST'])
def admin_login():
    if is_logged_in():
        return redirect(url_for('admin_dashboard'))
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            error = 'Wrong password. Try again.'
    return render_template('admin_login.html', error=error)

@app.route('/victor-admin/dashboard')
def admin_dashboard():
    if not is_logged_in():
        return redirect(url_for('admin_login'))
    products = load_products()
    emails   = load_emails()
    return render_template('admin_dashboard.html', products=products, emails=emails)

@app.route('/victor-admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

@app.route('/api/products', methods=['GET'])
def get_products():
    return jsonify([p for p in load_products() if p.get('available', True)])

@app.route('/api/products', methods=['POST'])
def add_product():
    if not is_logged_in():
        return jsonify({'success': False}), 401
    products = load_products()
    data = request.json
    sub_cat = data.get('sub_category', '')
    if data.get('category') == 'laptop' and not sub_cat:
        sub_cat = 'budget' if int(data.get('price', 0)) <= 250000 else 'pro'
    product = {
        'id': int(datetime.datetime.now().timestamp() * 1000),
        'name': data.get('name', ''),
        'price': int(data.get('price', 0)),
        'category': data.get('category', 'laptop'),
        'sub_category': sub_cat,
        'specs': data.get('specs', ''),
        'description': data.get('description', ''),
        'image': data.get('image', ''),
        'available': True,
        'created_at': datetime.datetime.now().isoformat()
    }
    products.append(product)
    save_products(products)
    return jsonify({'success': True, 'product': product})

@app.route('/api/products/<int:product_id>', methods=['PUT'])
def update_product(product_id):
    if not is_logged_in():
        return jsonify({'success': False}), 401
    products = load_products()
    data = request.json
    for i, p in enumerate(products):
        if p['id'] == product_id:
            products[i].update(data)
            save_products(products)
            return jsonify({'success': True})
    return jsonify({'success': False}), 404

@app.route('/api/products/<int:product_id>', methods=['DELETE'])
def delete_product(product_id):
    if not is_logged_in():
        return jsonify({'success': False}), 401
    products = [p for p in load_products() if p['id'] != product_id]
    save_products(products)
    return jsonify({'success': True})

@app.route('/api/upload_image', methods=['POST'])
def upload_image():
    if not is_logged_in():
        return jsonify({'success': False}), 401
    data = request.json
    img_b64 = data.get('image', '')
    ext = data.get('ext', 'jpg')
    fname = f"product_{int(datetime.datetime.now().timestamp() * 1000)}.{ext}"
    path = os.path.join('static', 'images', 'products', fname)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img_data = base64.b64decode(img_b64.split(',')[1] if ',' in img_b64 else img_b64)
    with open(path, 'wb') as f:
        f.write(img_data)
    return jsonify({'success': True, 'url': f'/static/images/products/{fname}'})

@app.route('/api/subscribe', methods=['POST'])
def subscribe():
    data = request.json
    email = data.get('email', '').strip()
    if not email or '@' not in email:
        return jsonify({'success': False}), 400
    emails = load_emails()
    if email not in emails:
        emails.append(email)
        save_emails(emails)
    return jsonify({'success': True})
@app.route('/catalog')
def catalog():
    products = [p for p in load_products() if p.get('available', True)]
    return render_template('catalog.html', products=products)

if __name__ == '__main__':
    app.run(debug=True, port=5050)
