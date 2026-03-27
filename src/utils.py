import tempfile
import os

def save_temp_file(uploaded_file) -> str:
    """Save an uploaded file to a temp dir with its ORIGINAL filename. Returns the full path."""
    tmp_dir = tempfile.mkdtemp()
    file_path = os.path.join(tmp_dir, uploaded_file.name)  # e.g. /tmp/xyz/Vaibhav Dhamal 4y.pdf
    uploaded_file.seek(0)
    with open(file_path, "wb") as f:
        f.write(uploaded_file.read())
    return file_path