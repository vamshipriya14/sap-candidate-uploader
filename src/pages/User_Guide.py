from pathlib import Path

import streamlit as st

from auth import require_login, show_navigation, show_user_profile

st.set_page_config(page_title="User Guide", page_icon="📘", layout="wide")

st.markdown(
    """
    <style>
    [data-testid="stSidebarNav"] { display: none !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

user = require_login()
show_user_profile(user)
show_navigation("user_guide")

st.title("User Guide")

guide_path = Path(__file__).resolve().parent.parent / "USER_GUIDE.md"
st.markdown(guide_path.read_text(encoding="utf-8"))
