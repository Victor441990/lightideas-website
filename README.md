# Light Ideas Technology — Website

## Folder Structure
```
lightideas-website/
├── app.py                  ← Flask backend (run this)
├── requirements.txt        ← Python packages
├── products.json           ← Auto-created when you add products
├── emails.json             ← Auto-created when people subscribe
├── templates/
│   ├── index.html          ← Main website
│   └── admin.html          ← Admin panel (coming soon)
├── static/
│   ├── css/style.css       ← All styles
│   ├── js/main.js          ← All JavaScript
│   └── images/logo.jpg     ← Your logo
```

## Setup in VSCode

1. Open this folder in VSCode
2. Open terminal (Ctrl + `)
3. Install Flask:
   ```
   pip install -r requirements.txt
   ```
4. Run the website:
   ```
   python app.py
   ```
5. Open browser at: http://localhost:5050

## Before Going Live

- Replace `pk_live_YOUR_PAYSTACK_PUBLIC_KEY` in static/js/main.js
  with your actual key from dashboard.paystack.com

## Admin Panel
Go to http://localhost:5050/admin to manage products.
