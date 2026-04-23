from utils import save_temp_file


def missing_upload_fields(data):
    required = {
        "jr_number": "JR Number",
        "first_name": "First Name",
        "last_name": "Last Name",
        "email": "Email",
        "phone": "Phone",
        "resume_file": "Resume",
    }
    missing = []
    for key, label in required.items():
        value = data.get(key)
        if value is None:
            missing.append(label)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(label)
            continue
    return missing


def upload_to_sap(bot, data):
    """
    Upload a single candidate using an already-started and logged-in SAPBot instance.

    Args:
        bot:  A SAPBot instance that has already called start() + wait_for_login()
        data: Dict with keys - jr_number, first_name, last_name, email, phone,
              country_code, country, resume_file (Streamlit UploadedFile)
    """
    missing = missing_upload_fields(data)
    if missing:
        raise ValueError(f"Missing required candidate data: {', '.join(missing)}")

    resume_path = save_temp_file(data["resume_file"])
    bot.upload_candidate({
        **data,
        "resume_path": resume_path
    })
