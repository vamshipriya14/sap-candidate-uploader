import streamlit as st
import pandas as pd
from resume_parser import parse_resume
from sap_bot import SAPBot
from uploader import upload_to_sap
from utils import save_temp_file
import time

st.set_page_config(page_title="Resume Upload", layout="wide")

# =========================
# 🔹 PAGE LAYOUT
# =========================
st.title("📄 Resume Upload to SAP")
st.caption("Upload resumes and candidate details for SAP processing")

# =========================
# 🔹 SESSION STATE INIT
# =========================
if "bot" not in st.session_state:
    st.session_state.bot = None
if "sap_ready" not in st.session_state:
    st.session_state.sap_ready = False
if "uploaded_files_store" not in st.session_state:
    st.session_state.uploaded_files_store = {}
if "upload_results" not in st.session_state:
    st.session_state.upload_results = []

# =========================
# 🔹 SAP CREDENTIALS & LOGIN
# =========================
with st.sidebar:
    st.header("⚙️ SAP Configuration")

    sap_username = st.text_input("SAP Username", type="default")
    sap_password = st.text_input("SAP Password", type="password")

    if st.button("Connect to SAP", key="connect_sap"):
        if sap_username and sap_password:
            with st.spinner("Connecting to SAP..."):
                try:
                    bot = SAPBot()
                    bot.start()
                    bot.login(sap_username, sap_password)
                    st.session_state.bot = bot
                    st.session_state.sap_ready = True
                    st.success("✅ Connected to SAP!")
                except Exception as e:
                    st.error(f"❌ Failed to connect to SAP: {str(e)}")
                    st.session_state.sap_ready = False
        else:
            st.warning("Please enter SAP credentials")

    if st.session_state.sap_ready:
        st.success("🟢 SAP Connected")
    else:
        st.info("🔴 Not connected to SAP")

# =========================
# 🔹 FILE UPLOAD & PARSE
# =========================
st.subheader("📤 Upload Resumes")

files = st.file_uploader(
    "Select resume files",
    type=["pdf", "docx"],
    accept_multiple_files=True,
    help="Upload PDF or DOCX files. Each resume must have a unique filename."
)

if files:
    # Deduplicate by filename
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

    # Parse resumes
    results = []
    progress = st.progress(0)
    status_text = st.empty()

    for i, file in enumerate(files):
        status_text.text(f"Processing {i+1}/{len(files)}: {file.name}")

        # Store file bytes for later upload
        file.seek(0)
        st.session_state.uploaded_files_store[file.name] = file.read()

        try:
            file.seek(0)
            data = parse_resume(file)
            results.append({
                "File Name": file.name,
                "First Name": data.get("first_name", ""),
                "Last Name": data.get("last_name", ""),
                "Email": data.get("email", ""),
                "Phone": data.get("phone", ""),
                "Country Code": data.get("country_code", "+91"),
                "Country": data.get("country", "India"),
                "JR Number": "",
                "Error": ""
            })
        except Exception as e:
            results.append({
                "File Name": file.name,
                "First Name": "",
                "Last Name": "",
                "Email": "",
                "Phone": "",
                "Country Code": "+91",
                "Country": "India",
                "JR Number": "",
                "Error": str(e)
            })

        progress.progress((i + 1) / len(files))

    status_text.empty()

    # =========================
    # 🔹 VALIDATION & EDIT TABLE
    # =========================
    st.subheader("📋 Review Candidate Data")

    df = pd.DataFrame(results)

    # Display editable dataframe
    edited_df = st.data_editor(
        df,
        use_container_width=True,
        key="candidates_editor",
        hide_index=False,
        column_config={
            "First Name": st.column_config.TextColumn("First Name", required=True),
            "Last Name": st.column_config.TextColumn("Last Name", required=True),
            "Email": st.column_config.TextColumn("Email", required=True),
            "Phone": st.column_config.TextColumn("Phone"),
            "JR Number": st.column_config.TextColumn("JR Number", required=True),
            "Country": st.column_config.SelectboxColumn(
                "Country",
                options=[
                    "India", "United States", "United Kingdom", "Australia",
                    "United Arab Emirates", "Singapore", "Malaysia",
                    "Germany", "France", "Japan"
                ]
            ),
            "Error": st.column_config.TextColumn("Error", disabled=True),
        }
    )

    # Validation
    df_validation = edited_df.copy()
    df_validation["Status"] = df_validation.apply(
        lambda x: "❌ Missing Data" if not x["First Name"] or not x["Email"] or not x["JR Number"] else "✅ OK",
        axis=1
    )

    invalid_count = len(df_validation[df_validation["Status"] == "❌ Missing Data"])
    if invalid_count:
        st.warning(f"⚠️ {invalid_count} candidate(s) have missing required data")

    # Display validation status
    with st.expander("✅ Status Summary"):
        status_df = df_validation[["File Name", "Status"]].copy()
        st.dataframe(status_df, use_container_width=True, hide_index=True)

    # =========================
    # 🔹 UPLOAD TO SAP
    # =========================
    col1, col2 = st.columns([1, 3])

    with col1:
        if st.button("📤 Upload to SAP", type="primary", key="upload_btn"):
            if not st.session_state.sap_ready:
                st.error("❌ SAP connection required. Please connect in the sidebar.")
            elif invalid_count > 0:
                st.error("❌ Please fix all missing data before uploading.")
            else:
                # Upload to SAP
                upload_progress = st.progress(0)
                upload_status = st.empty()
                upload_results = []

                for idx, row in edited_df.iterrows():
                    upload_status.text(f"Uploading {idx+1}/{len(edited_df)}: {row['File Name']}")

                    try:
                        if row["Error"]:
                            upload_results.append({
                                "File": row["File Name"],
                                "Status": "❌ SKIPPED",
                                "Message": f"Parse error: {row['Error']}"
                            })
                        else:
                            # Create upload data
                            file_bytes = st.session_state.uploaded_files_store.get(row["File Name"])
                            if file_bytes:
                                # Create a file-like object
                                import io
                                file_obj = io.BytesIO(file_bytes)
                                file_obj.name = row["File Name"]

                                upload_data = {
                                    "jr_number": row["JR Number"],
                                    "first_name": row["First Name"],
                                    "last_name": row["Last Name"],
                                    "email": row["Email"],
                                    "phone": row["Phone"],
                                    "country_code": row["Country Code"],
                                    "country": row["Country"],
                                    "resume_file": file_obj
                                }

                                upload_to_sap(st.session_state.bot, upload_data)
                                upload_results.append({
                                    "File": row["File Name"],
                                    "Status": "✅ UPLOADED",
                                    "Message": f"Candidate: {row['First Name']} {row['Last Name']}"
                                })

                    except Exception as e:
                        upload_results.append({
                            "File": row["File Name"],
                            "Status": "❌ FAILED",
                            "Message": str(e)
                        })

                    upload_progress.progress((idx + 1) / len(edited_df))

                upload_status.empty()
                st.session_state.upload_results = upload_results

                # Display results
                st.subheader("📊 Upload Results")
                results_df = pd.DataFrame(upload_results)
                st.dataframe(results_df, use_container_width=True, hide_index=True)

                success_count = len([r for r in upload_results if "✅" in r["Status"]])
                st.success(f"✅ {success_count}/{len(upload_results)} candidates uploaded successfully!")

    with col2:
        if st.session_state.upload_results:
            st.info("Last upload completed. Review results above or upload new resumes.")

else:
    st.info("👆 Upload resume files to get started")
