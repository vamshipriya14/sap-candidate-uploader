# Deployment Guide: Resume Upload Form on GoDaddy Linux Hosting

Deploy the SAP Resume Upload Streamlit app to `resume.yourcompany.com`

---

## Prerequisites

- ✅ GoDaddy Linux hosting with SSH access
- ✅ Domain pointing to your hosting (resume.yourcompany.com)
- ✅ Git installed on the server (or download ZIP)
- ✅ Python 3.9+ installed
- ✅ All required secrets (Supabase, GitHub, SAP credentials)

---

## Step 1: Connect to Your Server via SSH

```bash
ssh username@resume.yourcompany.com
# Or use the SSH details from your GoDaddy control panel
```

Enter your password when prompted.

---

## Step 2: Navigate to Your Project Directory

```bash
# Go to home directory (or wherever you want to store the app)
cd ~

# Create a project directory if needed
mkdir -p /home/username/sap-uploader
cd /home/username/sap-uploader
```

---

## Step 3: Clone/Download the Project

**Option A: Clone from GitHub**
```bash
git clone https://github.com/your-org/sap-candidate-uploader.git
cd sap-candidate-uploader
```

**Option B: Upload ZIP file**
```bash
# Download and extract via SCP or GoDaddy File Manager
unzip sap-candidate-uploader.zip
cd sap-candidate-uploader
```

---

## Step 4: Create Python Virtual Environment

```bash
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate
```

You should see `(venv)` in your terminal prompt.

---

## Step 5: Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Wait for all packages to install (~2-3 minutes).

---

## Step 6: Configure Secrets

### Create Streamlit secrets directory:
```bash
mkdir -p ~/.streamlit
nano ~/.streamlit/secrets.toml
```

### Add your secrets (paste this template and fill in your values):
```toml
# Supabase
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "your-supabase-service-role-key"

# GitHub (for triggering SAP workflow)
GH_REPO = "your-org/sap-candidate-uploader"
GH_TOKEN = "your-github-personal-access-token"
GH_EVENT_TYPE = "resume-form-submitted"

# User whitelist - ONLY these users can submit resumes via the form
# Comma-separated list of email addresses
ALLOWED_FORM_USERS = "john.doe@company.com,jane.smith@company.com,recruiter@company.com"

# (Optional) If using email features
INBOX_EMAIL = "your-inbox@company.com"
```

**Save and exit:** Press `Ctrl+X`, then `Y`, then `Enter`

### Managing the User Whitelist

To update who can submit forms, edit the secrets file:

```bash
nano ~/.streamlit/secrets.toml
```

Modify the `ALLOWED_FORM_USERS` line:
```toml
ALLOWED_FORM_USERS = "user1@company.com,user2@company.com,user3@company.com"
```

**Changes take effect immediately** — no restart needed (Streamlit reloads secrets).

To allow **anyone logged in** (no whitelist), leave it empty:
```toml
ALLOWED_FORM_USERS = ""
```

---

## Step 7: Test Streamlit Locally

```bash
# Make sure you're in the project directory and venv is activated
streamlit run src/pages/Resume_Upload.py --server.port 8501
```

You should see:
```
You can now view your Streamlit app in your browser.

Local URL: http://localhost:8501
Network URL: http://your-server-ip:8501
```

**Stop the server:** Press `Ctrl+C`

---

## Step 8: Create Startup Script

Create a script to run Streamlit in the background:

```bash
nano start_app.sh
```

Paste this content:
```bash
#!/bin/bash

# Navigate to app directory
cd /home/username/sap-uploader/sap-candidate-uploader

# Activate virtual environment
source venv/bin/activate

# Run Streamlit
streamlit run src/pages/Resume_Upload.py \
  --server.port 8501 \
  --server.address 0.0.0.0 \
  --logger.level=info
```

Make it executable:
```bash
chmod +x start_app.sh
```

---

## Step 9: Run Streamlit in Background (Using nohup)

```bash
nohup ./start_app.sh > streamlit.log 2>&1 &
```

Check if it's running:
```bash
ps aux | grep streamlit
```

You should see the process listed.

View logs:
```bash
tail -f streamlit.log
```

---

## Step 10: Configure Nginx Reverse Proxy (Recommended)

This allows you to access the app via `https://resume.yourcompany.com` with SSL.

### Check if Nginx is installed:
```bash
nginx -v
```

If not installed, ask GoDaddy support to enable it or install via:
```bash
sudo apt update
sudo apt install nginx
```

### Create Nginx config:

```bash
sudo nano /etc/nginx/sites-available/resume
```

Paste this configuration:
```nginx
server {
    listen 80;
    server_name resume.yourcompany.com;

    location / {
        proxy_pass http://localhost:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Enable the site:
```bash
sudo ln -s /etc/nginx/sites-available/resume /etc/nginx/sites-enabled/resume
sudo rm /etc/nginx/sites-enabled/default  # Remove default site if needed
```

### Test Nginx config:
```bash
sudo nginx -t
```

Should output: `syntax is ok` and `test is successful`

### Restart Nginx:
```bash
sudo systemctl restart nginx
```

---

## Step 11: Enable SSL (HTTPS)

### Option A: Using GoDaddy SSL Certificate (Recommended)

1. Log into GoDaddy hosting control panel
2. Go to **SSL Certificates**
3. Install/activate your SSL certificate
4. Note the certificate path (usually `/home/username/ssl/` or similar)

Update Nginx config:
```bash
sudo nano /etc/nginx/sites-available/resume
```

Modify to:
```nginx
server {
    listen 443 ssl;
    server_name resume.yourcompany.com;

    ssl_certificate /path/to/your/certificate.crt;
    ssl_certificate_key /path/to/your/key.key;

    location / {
        proxy_pass http://localhost:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name resume.yourcompany.com;
    return 301 https://$server_name$request_uri;
}
```

Restart Nginx:
```bash
sudo systemctl restart nginx
```

### Option B: Using Let's Encrypt (Free)

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d resume.yourcompany.com
```

Follow the prompts. Certbot will auto-update your Nginx config.

---

## Step 12: Verify Everything Works

1. **Check if Streamlit is running:**
   ```bash
   ps aux | grep streamlit
   ```

2. **Check Nginx status:**
   ```bash
   sudo systemctl status nginx
   ```

3. **Visit your domain in a browser:**
   ```
   https://resume.yourcompany.com
   ```

4. **Test the form:**
   - Log in with your credentials
   - Upload a test resume
   - Verify it saves to Supabase

---

## Step 13: Make Streamlit Auto-Start on Reboot

Create a systemd service:

```bash
sudo nano /etc/systemd/system/streamlit.service
```

Paste:
```ini
[Unit]
Description=Streamlit Resume Upload App
After=network.target

[Service]
Type=simple
User=username
WorkingDirectory=/home/username/sap-uploader/sap-candidate-uploader
Environment="PATH=/home/username/sap-uploader/sap-candidate-uploader/venv/bin"
ExecStart=/home/username/sap-uploader/sap-candidate-uploader/venv/bin/streamlit run src/pages/Resume_Upload.py --server.port 8501 --server.address 0.0.0.0
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable streamlit
sudo systemctl start streamlit
```

Check status:
```bash
sudo systemctl status streamlit
```

---

## Troubleshooting

### Issue: "Connection refused" or "Cannot access resume.yourcompany.com"

**Check if Streamlit is running:**
```bash
ps aux | grep streamlit
tail -f streamlit.log  # View error logs
```

**Restart Streamlit:**
```bash
pkill -f streamlit
nohup ./start_app.sh > streamlit.log 2>&1 &
```

---

### Issue: "504 Bad Gateway" on Nginx

**Check Nginx error log:**
```bash
sudo tail -f /var/log/nginx/error.log
```

**Ensure Streamlit is on port 8501:**
```bash
lsof -i :8501
```

**Restart Nginx:**
```bash
sudo systemctl restart nginx
```

---

### Issue: SSL Certificate Error

**Verify certificate path:**
```bash
ls -la /path/to/your/certificate.crt
ls -la /path/to/your/key.key
```

**Regenerate with Certbot:**
```bash
sudo certbot renew --force-renewal
```

---

### Issue: Form not saving data to Supabase

**Check if secrets are loaded:**
```bash
grep -i supabase ~/.streamlit/secrets.toml
```

**Verify Supabase connectivity:**
```bash
curl -H "apikey: YOUR_SUPABASE_KEY" https://YOUR_SUPABASE_URL/rest/v1/jr_master?limit=1
```

**Restart Streamlit:**
```bash
pkill -f streamlit
nohup ./start_app.sh > streamlit.log 2>&1 &
```

---

## Daily Operations

### View logs:
```bash
tail -f streamlit.log
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log
```

### Stop the app:
```bash
pkill -f streamlit
```

### Start the app:
```bash
nohup ./start_app.sh > streamlit.log 2>&1 &
```

### Restart Nginx:
```bash
sudo systemctl restart nginx
```

### Update code from GitHub:
```bash
cd /home/username/sap-uploader/sap-candidate-uploader
git pull origin main
# Restart Streamlit to apply changes
```

---

## Security Notes

✅ **Always use HTTPS** (enable SSL certificate)

✅ **Keep secrets in `~/.streamlit/secrets.toml`** — Never commit to git

✅ **Use strong SSH password** or SSH keys for server access

✅ **Restrict access to admin pages** — Add basic auth if needed:
```nginx
location /admin {
    auth_basic "Admin Area";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://localhost:8501;
}
```

---

## Next Steps

1. ✅ Deploy the app following steps 1-13
2. ✅ Test the form at `https://resume.yourcompany.com`
3. ✅ Verify resumes are saved to Supabase
4. ✅ Test GitHub Actions trigger for SAP upload
5. ✅ Monitor logs for errors
6. ✅ Share URL with recruiters

---

**Questions?** Check logs:
```bash
tail -f streamlit.log
sudo journalctl -u streamlit -f
```
