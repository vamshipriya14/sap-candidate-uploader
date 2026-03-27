import io
import streamlit as st
import pandas as pd
from resume_parser import parse_resume
from sap_bot import SAPBot
from uploader import upload_to_sap
from auth import require_login, show_user_profile
from notifier import send_upload_notification

st.set_page_config(page_title="Resume → SAP Upload", layout="wide")

# =========================
# 🔹 AUTH — must be first
# =========================
user = require_login()
show_user_profile(user)

st.title("📄 Resume → SAP Upload")
st.caption(f"Logged in as **{user['name']}** ({user['email']})")

# =========================
# 🔹 SESSION STATE INIT
# =========================
if "bot" not in st.session_state:
    st.session_state.bot = None
if "sap_ready" not in st.session_state:
    st.session_state.sap_ready = False
if "uploaded_files_store" not in st.session_state:
    st.session_state.uploaded_files_store = {}

# =========================
# 🔹 FILE UPLOAD & PARSE
# =========================
files = st.file_uploader(
    "Upload Resumes",
    type=["pdf", "docx"],
    accept_multiple_files=True,
    help="Each resume must have a unique filename. Duplicates will be ignored."
)

if not files:
    st.stop()

# Deduplicate by filename — keep first occurrence only
seen = set()
unique_files = []
for f in files:
    if f.name in seen:
        st.warning(f"⚠️ Duplicate file skipped: **{f.name}**")
    else:
        seen.add(f.name)
        unique_files.append(f)
files = unique_files

st.info(f"📂 {len(files)} resume(s) ready for processing")

results = []
progress = st.progress(0)

for i, file in enumerate(files):
    # Store file bytes so we can re-read later during upload
    file.seek(0)
    st.session_state.uploaded_files_store[file.name] = file.read()

    try:
        file.seek(0)
        data = parse_resume(file)
        results.append({
            "File Name":    file.name,
            "First Name":   data.get("first_name", ""),
            "Last Name":    data.get("last_name", ""),
            "Email":        data.get("email", ""),
            "Phone":        data.get("phone", ""),
            "Country Code": data.get("country_code", "+91"),
            "Country":      data.get("country", "India"),
            "JR Number":    "",
        })
    except Exception as e:
        results.append({
            "File Name": file.name,
            "First Name": "", "Last Name": "", "Email": "",
            "Phone": "", "Country Code": "", "Country": "",
            "JR Number": "", "Error": str(e)
        })

    progress.progress((i + 1) / len(files))

# =========================
# 🔹 VALIDATION & TABLE
# =========================
df = pd.DataFrame(results)
df["Status"] = df.apply(
    lambda x: "❌ Missing Data" if not x["First Name"] or not x["Email"] else "✅ OK",
    axis=1
)

invalid_count = len(df[df["Status"] == "❌ Missing Data"])
if invalid_count:
    st.warning(f"⚠️ {invalid_count} resumes need correction before upload")

st.subheader("✏️ Review & Edit Data")
edited_df = st.data_editor(
    df,
    num_rows="dynamic",
    use_container_width=True,
    disabled=["File Name", "Status"],
)

# Clean empty rows
edited_df = edited_df.dropna(how="all")
edited_df = edited_df[
    ~(edited_df[["First Name", "Last Name", "Email", "Phone"]]
      .fillna("").apply(lambda x: x.str.strip()).eq("").all(axis=1))
]

if edited_df.empty:
    st.warning("⚠️ No valid data to upload")
    st.stop()

# Validate JR Numbers filled
missing_jr = edited_df[edited_df["JR Number"].fillna("").str.strip() == ""]
if not missing_jr.empty:
    st.warning(f"⚠️ {len(missing_jr)} row(s) are missing JR Number — fill them before uploading")

# =========================
# 🔹 DOWNLOAD CSV
# =========================
csv = edited_df.to_csv(index=False).encode("utf-8")
st.download_button("📥 Download CSV", data=csv, file_name="parsed_resumes.csv", mime="text/csv")

st.divider()

# =========================
# 🔹 SAP SESSION CONTROL
# =========================
st.subheader("🤖 SAP Upload")

# Toggle is always visible so you can set mode before opening the browser
submit_mode = st.toggle(
    "✅ Submit candidates (Add Candidate)",
    value=False,
    help="ON = clicks 'Add Candidate' to submit | OFF = clicks 'Cancel' (dry run)"
)
if not submit_mode:
    st.caption("🧪 Dry run mode — form will be filled but **not submitted**")
else:
    st.caption("🚀 Live mode — candidates **will be submitted** to SAP")

st.write("")  # spacing

col1, col2 = st.columns([1, 2])

with col1:
    if not st.session_state.sap_ready:
        if st.button("🌐 Connect to SAP", use_container_width=True):
            with st.spinner("🔐 Logging in to SAP..."):
                try:
                    bot = SAPBot()
                    bot.start()
                    bot.login()
                    st.session_state.bot = bot
                    st.session_state.sap_ready = True
                    st.rerun()
                except Exception as e:
                    st.error(f"Login failed: {e}")
    else:
        st.success("✅ SAP session active")
        if st.button("🔒 Close SAP Session", use_container_width=True):
            st.session_state.bot.close()
            st.session_state.bot = None
            st.session_state.sap_ready = False
            st.rerun()

with col2:
    if not st.session_state.sap_ready:
        st.info("👆 Connect to SAP first to enable upload")
    else:
        if st.button("🚀 Upload All to SAP", type="primary", use_container_width=True):

            upload_rows = edited_df[
                (edited_df["Status"] == "✅ OK") &
                (edited_df["JR Number"].fillna("").str.strip() != "")
            ]

            if upload_rows.empty:
                st.error("❌ No valid rows with JR Number to upload")
            else:
                import io
                results_log = []
                upload_progress = st.progress(0)
                status_box = st.empty()

                for idx, (_, row) in enumerate(upload_rows.iterrows()):
                    status_box.info(f"⏳ Uploading {row['File Name']} ({idx+1}/{len(upload_rows)})...")
                    try:
                        file_bytes = st.session_state.uploaded_files_store.get(row["File Name"])
                        if not file_bytes:
                            raise Exception("File bytes not found in session — please re-upload")

                        file_obj = io.BytesIO(file_bytes)
                        file_obj.name = row["File Name"]

                        upload_to_sap(st.session_state.bot, {
                            "jr_number":    str(row["JR Number"]).strip(),
                            "first_name":   row["First Name"],
                            "last_name":    row["Last Name"],
                            "submit":       submit_mode,
                            "email":        row["Email"],
                            "phone":        row["Phone"],
                            "country_code": row["Country Code"],
                            "country":      row["Country"],
                            "resume_file":  file_obj,
                        })
                        results_log.append({"File": row["File Name"], "Status": "✅ Success"})

                    except Exception as e:
                        results_log.append({"File": row["File Name"], "Status": f"❌ {str(e)}"})

                    upload_progress.progress((idx + 1) / len(upload_rows))

                status_box.empty()

                results_df = pd.DataFrame(results_log)
                success = len(results_df[results_df["Status"] == "✅ Success"])
                failed  = len(results_df) - success

                if failed == 0:
                    st.success(f"🎉 All {success} candidates uploaded successfully!")
                else:
                    st.warning(f"✅ {success} succeeded   ❌ {failed} failed")

                st.dataframe(results_df, use_container_width=True)

                # Send email notification to logged-in user
                with st.spinner("📧 Sending upload report to your email..."):
                    ok, msg = send_upload_notification(
                        access_token=user["access_token"],
                        user=user,
                        results=results_log,
                        submit_mode=submit_mode
                    )
                if ok:
                    st.info(f"📧 Upload report sent to **{user['email']}**")
                else:
                    st.warning(f"⚠️ Could not send email: {msg}")