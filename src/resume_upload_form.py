"""
Simple Resume Upload Form
No SSO login required. Accepts resumes and collects candidate information.
"""
import streamlit as st
import pandas as pd
from resume_parser import parse_resume
import io

st.set_page_config(page_title="Resume Upload Form", layout="wide")

# =========================
# 🔹 PAGE HEADER
# =========================
st.title("📄 Resume Upload Form")
st.markdown("Submit your resume and candidate information")

# =========================
# 🔹 FORM SECTION
# =========================
col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("📤 Upload Your Resume")

    uploaded_file = st.file_uploader(
        "Select a resume file",
        type=["pdf", "docx"],
        help="Upload a PDF or DOCX resume"
    )

    if uploaded_file:
        # Parse resume automatically
        try:
            with st.spinner("Parsing resume..."):
                parsed_data = parse_resume(uploaded_file)
        except Exception as e:
            st.error(f"Failed to parse resume: {str(e)}")
            parsed_data = {
                "first_name": "",
                "last_name": "",
                "email": "",
                "phone": "",
                "country_code": "+91",
                "country": "India"
            }

        # Candidate Information Form
        st.subheader("👤 Candidate Information")

        with st.form("candidate_form"):
            col1, col2 = st.columns(2)

            with col1:
                first_name = st.text_input(
                    "First Name *",
                    value=parsed_data.get("first_name", ""),
                    help="Required"
                )
                email = st.text_input(
                    "Email *",
                    value=parsed_data.get("email", ""),
                    help="Required"
                )
                phone = st.text_input(
                    "Phone Number",
                    value=parsed_data.get("phone", ""),
                    help="Optional"
                )

            with col2:
                last_name = st.text_input(
                    "Last Name *",
                    value=parsed_data.get("last_name", ""),
                    help="Required"
                )
                jr_number = st.text_input(
                    "Job Requisition (JR) Number *",
                    help="Required - SAP Job Requisition Number"
                )
                country = st.selectbox(
                    "Country",
                    options=[
                        "India", "United States", "United Kingdom", "Australia",
                        "United Arab Emirates", "Singapore", "Malaysia",
                        "Germany", "France", "Japan", "Other"
                    ],
                    index=0 if parsed_data.get("country") == "India" else 10
                )

            st.divider()

            col1, col2, col3 = st.columns(3)
            with col1:
                submitted = st.form_submit_button("✅ Submit", type="primary")

            with col2:
                st.form_submit_button("🔄 Reset Form")

        # Process form submission
        if submitted:
            # Validation
            errors = []
            if not first_name.strip():
                errors.append("First Name is required")
            if not last_name.strip():
                errors.append("Last Name is required")
            if not email.strip():
                errors.append("Email is required")
            if not jr_number.strip():
                errors.append("JR Number is required")

            if errors:
                st.error("Please fix the following errors:")
                for error in errors:
                    st.error(f"  • {error}")
            else:
                # Success
                st.success("✅ Form submitted successfully!")

                # Display summary
                col1, col2 = st.columns(2)

                with col1:
                    st.subheader("📋 Submitted Information")
                    summary = {
                        "First Name": first_name,
                        "Last Name": last_name,
                        "Email": email,
                        "Phone": phone or "Not provided",
                        "JR Number": jr_number,
                        "Country": country,
                        "Resume": uploaded_file.name
                    }
                    for key, value in summary.items():
                        st.write(f"**{key}:** {value}")

                with col2:
                    st.subheader("📊 Parse Confidence")
                    confidence_levels = {
                        "first_name": parsed_data.get("first_name") != "" and "✅" or "❌",
                        "last_name": parsed_data.get("last_name") != "" and "✅" or "❌",
                        "email": parsed_data.get("email") != "" and "✅" or "❌",
                        "phone": parsed_data.get("phone") != "" and "✅" or "❌",
                    }
                    st.write(f"{confidence_levels['first_name']} First Name parsed")
                    st.write(f"{confidence_levels['last_name']} Last Name parsed")
                    st.write(f"{confidence_levels['email']} Email parsed")
                    st.write(f"{confidence_levels['phone']} Phone parsed")

                # Option to submit to backend (can be customized)
                st.info(
                    "📌 Next: This form data would typically be sent to your backend "
                    "for processing and SAP integration."
                )

    else:
        st.info("👆 Upload a resume file to begin")

with col2:
    st.subheader("ℹ️ Instructions")
    st.markdown("""
    1. **Upload** your resume (PDF or DOCX)
    2. **Review** the auto-filled information
    3. **Correct** any details if needed
    4. **Enter** the Job Requisition (JR) number
    5. **Submit** the form

    ### Supported File Types
    - 📄 PDF (.pdf)
    - 📝 Word (.docx)
    """)

    st.divider()

    st.subheader("✨ Features")
    st.markdown("""
    - 🤖 Auto-parse resume data
    - ✏️ Edit extracted information
    - 🌍 Select country/location
    - ⚡ Instant validation
    - 📱 Mobile friendly
    """)
