# Deploying to GoDaddy Deluxe (No VPS / No SSH)

## Architecture Overview

```
GoDaddy subdomain (static files only)          Render.com free tier (Python backend)
  upload.yourdomain.com                          sap-resume-api.onrender.com
  ┌─────────────────────┐                        ┌──────────────────────────────┐
  │  index.html         │ ──── API calls ────►   │  app.py (Flask)              │
  │  (pure HTML/JS/CSS) │                        │  • /api/jr-master            │
  │                     │                        │  • /api/parse-resume ← spaCy │
  └─────────────────────┘                        │  • /api/upload-resume        │
                                                  │  • /api/submit-candidates    │
                                                  └──────────────────────────────┘
                                                             │
                                                    Supabase + GitHub Actions
```

**Why this works without VPS:**
- GoDaddy Deluxe hosting serves static files (HTML/CSS/JS) fine
- Python cannot run on GoDaddy shared hosting — so the backend lives on Render.com (free)
- The real Python resume parser (spaCy + pdfplumber + phonenumbers) runs on Render
- The frontend calls the Render API for all parsing and database operations

---

## Step 1 — Deploy the Backend to Render.com (FREE)

1. Create a free account at https://render.com

2. Create a new GitHub repo (or use your existing one) and push the `backend/` folder to it.
   The `backend/` folder must contain: `app.py`, `requirements.txt`, `render.yaml`

3. In Render dashboard → **New** → **Web Service** → connect your GitHub repo

4. Render will auto-detect `render.yaml` and configure the service. Settings:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`
   - **Plan**: Free

5. Set the following **Environment Variables** in Render dashboard:

   | Variable                  | Value                                    |
   |---------------------------|------------------------------------------|
   | `SUPABASE_URL`            | `https://your-project.supabase.co`       |
   | `SUPABASE_SERVICE_ROLE_KEY` | your Supabase service role key          |
   | `GH_REPO`                 | `your-org/your-repo`                     |
   | `GH_TOKEN`                | your GitHub PAT (repo + workflow scope)  |
   | `GH_EVENT_TYPE`           | `resume-form-submitted`                  |
   | `ALLOWED_ORIGINS`         | `https://upload.yourdomain.com`          |
   | `FLASK_ENV`               | `production`                             |

6. Deploy. Note your service URL, e.g. `https://sap-resume-api.onrender.com`

> ⚠️ **Free tier cold starts**: Render's free tier spins down after 15 min of inactivity.
> First request after idle may take ~30 seconds. This is fine for this use case.
> If you need instant cold starts, upgrade to Render's $7/mo plan.

---

## Step 2 — Configure the Frontend

1. Open `frontend/index.html`

2. Find this line near the top of the `<script>` section:
   ```js
   const API_BASE = "%%BACKEND_URL%%";
   ```

3. Replace `%%BACKEND_URL%%` with your Render URL:
   ```js
   const API_BASE = "https://sap-resume-api.onrender.com";
   ```

4. Save the file.

---

## Step 3 — Upload to GoDaddy via File Manager

**Option A — cPanel File Manager (easiest, no SSH):**

1. Log in to your GoDaddy account → My Products → Hosting → Manage
2. Open **cPanel** → **File Manager**
3. Navigate to the folder for your subdomain:
   - If subdomain is `upload.yourdomain.com`, the folder is usually `public_html/upload/`
   - Or create a new folder in `public_html/` if needed
4. Upload `frontend/index.html` into that folder
5. Done — visit `https://upload.yourdomain.com`

**Option B — FTP (FileZilla):**

1. In cPanel → FTP Accounts, create an FTP account
2. Connect via FileZilla using your GoDaddy FTP credentials
3. Upload `frontend/index.html` to the subdomain's folder

**Option C — Use .cpanel.yml Git Deployment** (if your GoDaddy supports it):

Your project already has `.cpanel.yml`. You can set up cPanel's Git Version Control
to auto-deploy when you push to GitHub. Ask GoDaddy support to enable this.

---

## Step 4 — Create the Subdomain in GoDaddy

1. GoDaddy dashboard → Domains → your domain → DNS
2. Add a **CNAME** or **A record** for `upload.yourdomain.com`
   pointing to your cPanel hosting IP (find it in cPanel → Stats → Shared IP Address)
3. In cPanel → **Subdomains** → create `upload` pointing to the correct folder

---

## Verification Checklist

- [ ] `https://sap-resume-api.onrender.com/api/health` returns `{"status": "ok", "nlp_loaded": true}`
- [ ] `https://upload.yourdomain.com` loads the upload form
- [ ] JR dropdown populates from Supabase
- [ ] Uploading a PDF/DOCX parses name, email, phone correctly
- [ ] Submitting creates a record in Supabase `candidates_submitted` table
- [ ] GitHub Actions workflow fires and processes the SAP upload

---

## Functionality Preserved from Streamlit

| Streamlit Feature                     | Web App Equivalent                        |
|---------------------------------------|-------------------------------------------|
| JR selector with skill info           | ✅ Dropdown loads from Supabase           |
| Multi-file resume upload              | ✅ Drag-and-drop + file picker            |
| Python resume parser (spaCy, etc.)    | ✅ Server-side via `/api/parse-resume`    |
| Editable candidate table              | ✅ Inline editable cells                  |
| Duplicate check                       | ✅ Checked at submit via Supabase         |
| Upload to Supabase Storage            | ✅ Via `/api/upload-resume`               |
| Insert to Supabase DB                 | ✅ Via `/api/submit-candidates`           |
| Trigger GitHub Actions                | ✅ repository_dispatch on submit          |
| Submission summary                    | ✅ Summary panel after submit             |
| Public access mode (`?public=true`)   | ✅ No auth required on this web version  |

---

## Troubleshooting

**CORS error in browser console:**
- Add your GoDaddy subdomain URL to `ALLOWED_ORIGINS` env var on Render

**JR list not loading:**
- Check Render logs (`render.com` → your service → Logs)
- Verify `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are correct

**spaCy model not loading:**
- Ensure the spaCy model URL is in `requirements.txt`
- Check Render build logs for installation errors

**Resume parse returns empty:**
- The file may be image-only (scanned PDF with no text layer)
- pdfplumber only extracts digital text, not OCR

**Render cold start slow:**
- Normal on free tier. Consider using UptimeRobot (free) to ping `/api/health`
  every 14 minutes to keep the service warm.
