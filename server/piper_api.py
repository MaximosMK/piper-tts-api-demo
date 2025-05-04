import subprocess
import os
from flask import Flask, request, jsonify, send_file, Response
import io
import logging # Added for better logging

# --- Configuration ---
# IMPORTANT: Set these paths correctly for your Codespace environment!
# Adjust 'YOUR_REPO_NAME' if your GitHub repository name is different.
CODESPACE_WORKSPACE_PATH = f"/workspaces/{os.environ.get('RepositoryName', 'piper-api-server')}" # Try to get repo name automatically

PIPER_EXECUTABLE = os.path.join(CODESPACE_WORKSPACE_PATH, 'piper', 'piper')
MODEL_PATH = os.path.join(CODESPACE_WORKSPACE_PATH, 'piper', 'en_US-hfc_male-medium.onnx') # Using the hfc_male model
CONFIG_PATH = os.path.join(CODESPACE_WORKSPACE_PATH, 'piper', 'en_US-hfc_male-medium.onnx.json') # Using the hfc_male config
# --- End Configuration ---

app = Flask(__name__)

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Basic check to see if the server is running
@app.route('/')
def index():
    logging.info("Index route '/' accessed.")
    # Check if Piper executable exists
    piper_exists = os.path.exists(PIPER_EXECUTABLE)
    model_exists = os.path.exists(MODEL_PATH)
    config_exists = os.path.exists(CONFIG_PATH)
    return f"""
    Piper TTS API is running!<br>
    Piper Executable ({PIPER_EXECUTABLE}): {'Found' if piper_exists else 'NOT FOUND!'}<br>
    Model File ({MODEL_PATH}): {'Found' if model_exists else 'NOT FOUND!'}<br>
    Config File ({CONFIG_PATH}): {'Found' if config_exists else 'NOT FOUND!'}<br>
    <br>
    Ensure the paths in piper_api.py are correct and the files exist in the Codespace.
    """

# The main endpoint for synthesis
@app.route('/synthesize', methods=['POST'])
def synthesize():
    logging.info("Received request on /synthesize")
    if not request.is_json:
        logging.warning("Request is not JSON")
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json()
    text = data.get('text')

    if not text:
        logging.warning("Missing 'text' in JSON payload")
        return jsonify({"error": "Missing 'text' in JSON payload"}), 400

    # --- Input Validation (Optional but Recommended) ---
    # Add checks for text length, allowed characters, etc. if needed
    logging.info(f"Received text (first 50 chars): '{text[:50]}...'")

    # --- Run Piper ---
    # We will pipe the text to stdin and capture the WAV audio from stdout
    command = [
        PIPER_EXECUTABLE,
        '--model', MODEL_PATH,
        '--config', CONFIG_PATH,
        '--output_file', '-' # Output WAV data to stdout
    ]

    try:
        logging.info(f"Running Piper command: {' '.join(command)}")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Send text to Piper (encode to bytes) and close stdin
        stdout_data, stderr_data = process.communicate(input=text.encode('utf-8'))

        # Check for errors
        if process.returncode != 0:
            error_message = stderr_data.decode('utf-8', errors='ignore').strip()
            logging.error(f"Piper Error (Code {process.returncode}): {error_message}")
            return jsonify({"error": f"Piper failed: {error_message}"}), 500

        if not stdout_data:
             logging.error("Piper Error: No audio data received from stdout.")
             return jsonify({"error": "Piper produced no audio data"}), 500

        logging.info(f"Piper ran successfully, sending {len(stdout_data)} bytes of audio.")

        # Send the raw WAV audio data back
        audio_io = io.BytesIO(stdout_data)
        return send_file(
            audio_io,
            mimetype='audio/wav',
            as_attachment=False # Send inline, not as a download
        )

    except FileNotFoundError:
        logging.critical(f"Error: Piper executable not found at {PIPER_EXECUTABLE}")
        return jsonify({"error": f"Server configuration error: Piper executable not found"}), 500
    except Exception as e:
        logging.exception(f"Unexpected server error during synthesis") # Log full traceback
        return jsonify({"error": f"An unexpected error occurred on the server: {e}"}), 500

if __name__ == '__main__':
    # Listen on all network interfaces (0.0.0.0) and port 5100
    # Use a port number above 1024 unless running as root (not recommended)
    logging.info("Starting Flask server on host 0.0.0.0, port 5100")
    app.run(host='0.0.0.0', port=5100, debug=False) # Turn debug=False for production