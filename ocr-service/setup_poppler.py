import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

# URL and paths
POPPLER_URL = "https://github.com/oschwartz10612/poppler-windows/releases/download/v24.08.0-0/Release-24.08.0-0.zip"
PROJECT_DIR = Path(__file__).resolve().parent
BIN_DIR = PROJECT_DIR / "bin"
ZIP_PATH = PROJECT_DIR / "poppler.zip"

def main():
    print("=" * 60)
    print("Banca OCR — Auto Poppler Setup Script")
    print("=" * 60)

    # 1. Create bin directory
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Download Poppler
    print(f"Downloading Poppler from: {POPPLER_URL}...")
    try:
        urllib.request.urlretrieve(POPPLER_URL, ZIP_PATH)
        print("Download completed successfully.")
    except Exception as e:
        print(f"Error downloading Poppler: {e}")
        return

    # 3. Extract Poppler
    print("Extracting Poppler archive...")
    try:
        with zipfile.ZipFile(ZIP_PATH, 'r') as zip_ref:
            zip_ref.extractall(BIN_DIR)
        print("Extraction completed.")
    except Exception as e:
        print(f"Error extracting ZIP: {e}")
        # Clean up
        if ZIP_PATH.exists():
            ZIP_PATH.unlink()
        return

    # 4. Rename folder to 'poppler'
    print("Configuring directories...")
    try:
        # Find folder starting with poppler- or Release-
        extracted_dirs = [
            d for d in BIN_DIR.iterdir() 
            if d.is_dir() and (d.name.startswith("Release-") or d.name.startswith("poppler-"))
        ]
        if extracted_dirs:
            src_dir = extracted_dirs[0]
            dest_dir = BIN_DIR / "poppler"
            
            # Remove destination if it exists
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
                
            src_dir.rename(dest_dir)
            print(f"Successfully configured Poppler at: {dest_dir}")
        else:
            print("Warning: Could not find extracted folder starting with 'Release-' or 'poppler-'. Please check the 'bin' folder.")
    except Exception as e:
        print(f"Error configuring directory name: {e}")

    # 5. Clean up ZIP file
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
        print("Cleaned up temporary archive.")
        
    print("=" * 60)
    print("Setup finished! You can now restart your uvicorn server.")
    print("=" * 60)

if __name__ == "__main__":
    main()
