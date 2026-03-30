import os
import streamlit as st
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv
from streamlit.errors import StreamlitSecretNotFoundError

load_dotenv()


def _secret(name: str, *fallback_names: str, default: str = "") -> str:
    secrets_obj = None
    try:
        secrets_obj = st.secrets
    except StreamlitSecretNotFoundError:
        secrets_obj = None
    except Exception:
        secrets_obj = None

    for key in (name, *fallback_names):
        if secrets_obj is not None:
            try:
                value = secrets_obj.get(key)
                if value:
                    return str(value)
            except StreamlitSecretNotFoundError:
                pass
            except Exception:
                pass

    for key in (name, *fallback_names):
        value = os.getenv(key)
        if value:
            return value

    return default


CLIENT_ID = _secret("ST_AZURE_CLIENT_ID")
CLIENT_SECRET = _secret("ST_AZURE_CLIENT_SECRET")
TENANT_ID = _secret("ST_AZURE_TENANT_ID")
REDIRECT_URI = _secret("ST_AZURE_REDIRECT_URI", default="http://localhost:8501")

AUTH_URL  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

# User-level sign-in plus OneDrive delegated upload scope.
SCOPE = "User.Read Files.ReadWrite MailboxSettings.Read openid profile email"


# =========================
# 🔹 LOGIN PAGE UI
# =========================
def show_login_page():
    if not CLIENT_ID or not CLIENT_SECRET or not TENANT_ID or not REDIRECT_URI:
        st.error("Missing Streamlit SSO secrets. Configure ST_AZURE_TENANT_ID, ST_AZURE_CLIENT_ID, ST_AZURE_CLIENT_SECRET, and ST_AZURE_REDIRECT_URI.")
        st.stop()

    st.markdown("""
        <div style='text-align:center; padding: 60px 0 20px'>
            <h1>📄 Resume → SAP Upload</h1>
            <p style='color: gray; font-size: 16px'>Sign in with your Microsoft account to continue</p>
        </div>
    """, unsafe_allow_html=True)

    params = {
        "client_id":     CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  REDIRECT_URI,
        "response_mode": "query",
        "scope":         SCOPE,
        "prompt":        "select_account",   # always show account picker
    }
    login_url = f"{AUTH_URL}?{urlencode(params)}"

    # Centered button
    col = st.columns([1, 2, 1])[1]
    with col:
        st.link_button(
            "🔐  Sign in with Microsoft",
            url=login_url,
            width="stretch",
            type="primary"
        )
    st.stop()


# =========================
# 🔹 TOKEN EXCHANGE
# =========================
def _exchange_code_for_token(code: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope":         SCOPE,
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    })
    return resp.json()


# =========================
# 🔹 FETCH USER PROFILE
# =========================
def _fetch_user(access_token: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    user = requests.get(
        "https://graph.microsoft.com/v1.0/me"
        "?$select=displayName,mail,userPrincipalName,jobTitle,department,officeLocation",
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

    # Fetch email signature
    signature = None
    sig_resp = requests.get(
        "https://graph.microsoft.com/v1.0/me/mailboxSettings",
        headers=headers
    )
    if sig_resp.status_code == 200:
        data = sig_resp.json()
        # Common locations for user's email signature in Graph API
        signature = data.get("signature", "") or data.get("automaticRepliesSetting", {}).get("externalReplyMessage", "")
    
    return {
        "name":       user.get("displayName", ""),
        "email":      user.get("mail") or user.get("userPrincipalName", ""),
        "job_title":  user.get("jobTitle", ""),
        "department": user.get("department", ""),
        "office":     user.get("officeLocation", ""),
        "photo_b64":  photo_b64,
        "signature":  signature,
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
        return st.session_state.user

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
