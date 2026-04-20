import os
import streamlit as st
import time
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv
from streamlit.errors import StreamlitSecretNotFoundError

load_dotenv()


def _secret(name: str, *fallback_names: str) -> str:
    # Try Streamlit secrets first
    try:
        import streamlit as st
        from streamlit.errors import StreamlitSecretNotFoundError
        try:
            secrets_obj = st.secrets
            for key in (name, *fallback_names):
                try:
                    value = secrets_obj.get(key)
                    if value:
                        return str(value)
                except Exception:
                    pass
        except StreamlitSecretNotFoundError:
            pass
        except Exception:
            pass
    except ImportError:
        pass

    # Fall back to environment variables (GitHub Actions)
    for key in (name, *fallback_names):
        value = os.environ.get(key)
        if value:
            return value

    return ""

CLIENT_ID = _secret("ST_AZURE_CLIENT_ID")
CLIENT_SECRET = _secret("ST_AZURE_CLIENT_SECRET")
TENANT_ID = _secret("ST_AZURE_TENANT_ID")
REDIRECT_URI = _secret("ST_AZURE_REDIRECT_URI", default="http://localhost:8501")

AUTH_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

# User-level sign-in plus OneDrive delegated upload scope.
# offline_access is needed for refresh_token to keep user logged in longer.
SCOPE = "User.Read Files.ReadWrite openid profile email offline_access"


# =========================
# 🔹 LOGIN PAGE UI
# =========================
def show_login_page():
    if not CLIENT_ID or not CLIENT_SECRET or not TENANT_ID or not REDIRECT_URI:
        st.error(
            "Missing Streamlit SSO secrets. Configure ST_AZURE_TENANT_ID, ST_AZURE_CLIENT_ID, ST_AZURE_CLIENT_SECRET, and ST_AZURE_REDIRECT_URI.")
        st.stop()

    # -----------------------
    # HEADER
    # -----------------------
    st.markdown(
        """
        <div style="text-align:center;">
            <h1 style="margin-bottom:0;">📊 VoliATS</h1>
            <p style="color:gray; margin-top:5px;">
                Candidate Submission Pipeline
            </p>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.markdown("---")

    # -----------------------
    # LOGIN URL (KEEP THIS LOGIC SAME)
    # -----------------------
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPE,
        "prompt": "select_account",
    }

    login_url = f"{AUTH_URL}?{urlencode(params)}"

    # -----------------------
    # CENTER CARD
    # -----------------------
    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        st.markdown("""
        <div style="
            background-color:#0e1117;
            padding:25px;
            border-radius:12px;
            border:1px solid #262730;
        ">

        <h3>🚀 What you can do</h3>

        <ul>
        <li>📤 Upload resumes & auto-parse details</li>
        <li>✏️ Track candidate pipeline</li>
        <li>📊 Manage records efficiently</li>
        <li>📤 Upload directly to SAP</li>
        <li>📧 Send recruiter emails</li>
        <li>🔐 Secure Microsoft SSO login</li>
        </ul>

        </div>
        """, unsafe_allow_html=True)

        st.markdown("---")

        st.link_button(
            "🔐 Sign in with Microsoft",
            url=login_url,
            type="primary",
            use_container_width=True
        )

        st.caption("⚠️ Only @volibits.com accounts are allowed")

        st.markdown("</div>", unsafe_allow_html=True)

    # -----------------------
    # FOOTER
    # -----------------------
    st.markdown(
        """
        <div style="text-align:center; margin-top:30px; font-size:12px; color:gray;">
            © 2026 Volibits · Internal HR System
        </div>
        """,
        unsafe_allow_html=True
    )

    st.stop()


# =========================
# 🔹 TOKEN EXCHANGE
# =========================
def _exchange_code_for_token(code: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": SCOPE,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })
    return resp.json()


def _refresh_access_token(refresh_token: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": SCOPE,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    return resp.json()


# =========================
# 🔹 FETCH USER PROFILE
# =========================
def _fetch_user(access_token: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    user = requests.get(
        "https://graph.microsoft.com/v1.0/me"
        "?$select=displayName,mail,userPrincipalName,jobTitle,department,officeLocation,mobilePhone,businessPhones",
        headers=headers
    ).json()

    # Fetch profile photo (returns bytes or None)
    photo_resp = requests.get(
        "https://graph.microsoft.com/v1.0/me/photo/$value",
        headers=headers
    )
    photo_b64 = None
    if photo_resp.status_code == 200:
        import base64
        photo_b64 = base64.b64encode(photo_resp.content).decode()

    # Fetch email signature - skip as it requires MailboxSettings.Read (Admin consent)
    signature = None

    # Try to get phone
    phone = user.get("mobilePhone")
    if not phone and user.get("businessPhones"):
        phone = user.get("businessPhones")[0]

    return {
        "name": user.get("displayName", ""),
        "email": user.get("mail") or user.get("userPrincipalName", ""),
        "job_title": user.get("jobTitle", ""),
        "department": user.get("department", ""),
        "office": user.get("officeLocation", ""),
        "phone": phone or "",
        "photo_b64": photo_b64,
        "signature": signature,
    }


# =========================
# 🔹 MAIN AUTH GATE
# =========================
def require_login() -> dict:
    """
    Call at the top of app.py.
    Returns user dict if authenticated, otherwise shows login page and stops.
    User dict keys: name, email, job_title, department, office, photo_b64, access_token
    """
    # Already logged in this session
    if st.session_state.get("user"):
        user = st.session_state.user
        expires_at = st.session_state.get("token_expires_at", 0)
        refresh_token = st.session_state.get("refresh_token")
        last_refresh_attempt = st.session_state.get("last_refresh_attempt", 0)

        now = time.time()

        # Only attempt refresh when:
        #   1. Token expires within 10 minutes
        #   2. We have a refresh token
        #   3. We haven't tried a refresh in the last 60 seconds (debounce)
        #      — prevents hammering Azure on every Streamlit rerun
        needs_refresh = (
                refresh_token
                and now > (expires_at - 600)  # 10 min before expiry
                and now > (last_refresh_attempt + 60)  # debounce: max once per minute
        )

        if needs_refresh:
            st.session_state.last_refresh_attempt = now  # mark attempt before calling
            token_data = _refresh_access_token(refresh_token)

            if "access_token" in token_data:
                user["access_token"] = token_data["access_token"]
                st.session_state.user = user
                st.session_state.token_expires_at = now + token_data.get("expires_in", 3600)
                if "refresh_token" in token_data:
                    st.session_state.refresh_token = token_data["refresh_token"]
            else:
                # ── Refresh failed: DO NOT log the user out immediately.
                #    The existing access_token may still be valid for a while.
                #    Only force re-login once the token is actually expired. ──
                error = token_data.get("error", "")
                if now > expires_at or error in ("invalid_grant", "interaction_required"):
                    # Token is genuinely expired and unrefreshable — must re-login
                    st.session_state.user = None
                    st.session_state.refresh_token = None
                    st.session_state.token_expires_at = 0
                    st.rerun()
                # else: token still valid, silently continue with existing token

        return user

    # OAuth callback — code in query params
    code = st.query_params.get("code")
    if code:
        with st.spinner("🔐 Signing you in..."):
            token_data = _exchange_code_for_token(code)

        if "access_token" not in token_data:
            st.error(f"Authentication failed: {token_data.get('error_description', token_data)}")
            st.stop()

        user = _fetch_user(token_data["access_token"])
        user["access_token"] = token_data["access_token"]

        st.session_state.user = user
        st.session_state.refresh_token = token_data.get("refresh_token")
        st.session_state.token_expires_at = time.time() + token_data.get("expires_in", 3600)
        st.session_state.last_refresh_attempt = 0

        # Clean the code from URL so refresh doesn't re-exchange
        st.query_params.clear()
        st.rerun()

    # Not logged in — show login page
    show_login_page()


# =========================
# 🔹 LOGOUT
# =========================
def logout():
    st.session_state.user = None
    st.session_state.bot = None
    st.session_state.sap_ready = False
    st.query_params.clear()
    st.rerun()


# =========================
# 🔹 USER PROFILE WIDGET
# =========================
def show_navigation(current_page: str) -> None:
    """
    Renders page navigation buttons in the sidebar.
    current_page: 'new_records' or 'pending_emails' or 'user_guide'
    """
    # CSS: make the active-page button look like a primary/red button
    st.sidebar.markdown(
        """
        <style>
        div[data-testid="stSidebarContent"] .nav-active button {
            background-color: #E03C3C !important;
            color: white !important;
            border: none !important;
        }
        div[data-testid="stSidebarContent"] .nav-active button:hover {
            background-color: #c03030 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.markdown("#### 📋 Actions")
        # New Records Submission
        if current_page == "new_records":
            st.markdown('<div class="nav-active">', unsafe_allow_html=True)
        if st.button("📄 Resume Pipeline", use_container_width=True, key="nav_new_records"):
            st.switch_page("app_headless.py")
        if current_page == "new_records":
            st.markdown("</div>", unsafe_allow_html=True)

        # Pending Client Emails
        if current_page == "pending_emails":
            st.markdown('<div class="nav-active">', unsafe_allow_html=True)
        if st.button("📧 Client Emails", use_container_width=True, key="nav_pending_emails"):
            st.switch_page("pages/Pending_Client_Emails.py")
        if current_page == "pending_emails":
            st.markdown("</div>", unsafe_allow_html=True)

        # Email Inbox Sync
        if current_page == "email_inbox":
            st.markdown('<div class="nav-active">', unsafe_allow_html=True)
        if st.button("📬 Email Inbox Sync", use_container_width=True, key="nav_email_inbox"):
            st.switch_page("pages/Email_Inbox.py")
        if current_page == "email_inbox":
            st.markdown("</div>", unsafe_allow_html=True)

        # User Guide
        if current_page == "user_guide":
            st.markdown('<div class="nav-active">', unsafe_allow_html=True)
        if st.button("📘 User Guide", use_container_width=True, key="nav_user_guide"):
            st.switch_page("pages/User_Guide.py")
        if current_page == "user_guide":
            st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("---")


def show_user_profile(user: dict):
    """Renders a compact profile card in the sidebar."""
    with st.sidebar:
        st.markdown("---")
        if user.get("photo_b64"):
            st.markdown(
                f"<img src='data:image/jpeg;base64,{user['photo_b64']}' "
                f"style='border-radius:50%; width:64px; height:64px; display:block; margin:0 auto 8px'>",
                unsafe_allow_html=True
            )
        st.markdown(f"**{user['name']}**")
        st.caption(user["email"])
        if user.get("job_title"):
            st.caption(f"🏷️ {user['job_title']}")
        if user.get("department"):
            st.caption(f"🏢 {user['department']}")
        st.markdown("---")
        if st.button("🚪 Sign out", use_container_width=True):
            logout()