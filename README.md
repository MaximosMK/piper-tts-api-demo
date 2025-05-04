# Piper TTS API Client and Server

This project provides a Text-to-Speech (TTS) system using [Piper TTS](https://github.com/rhasspy/piper), exposed via a Flask/Gunicorn API server and controlled by a PySide6 GUI client. It demonstrates how to handle large text files by chunking them and processing them in parallel via the API for improved speed.

## Features

*   **API Server:** A Flask application (`server/piper_api.py`) that takes text input and returns WAV audio using Piper. Designed to run on a server (local machine, cloud VM, GitHub Codespace, etc.).
*   **GUI Client:** A PySide6 application (`client/piper_api_gui.py`) that allows users to:
    *   Load text from a file.
    *   Specify the API server URL.
    *   Specify an output WAV file path.
    *   Send large text files to the API in chunks for synthesis.
    *   Process chunks in parallel using `ThreadPoolExecutor` for faster results.
    *   Combine the resulting audio chunks into a single WAV file.
    *   View status logs and progress.

## Setup and Running

This project consists of two main parts: the **Server** (which runs Piper) and the **Client** (the GUI you interact with). You need to set up and run the server first.

### Part 1: Setting up the API Server (`server` directory)

This component runs Piper TTS and exposes it as an API. Run these steps on the machine where you want the TTS processing to happen (e.g., your local machine, a server, or a GitHub Codespace).

1.  **Clone this Repository:**
    ```bash
    git clone https://github.com/MaximosMK/piper-tts-api-demo.git
    cd piper-tts-api-demo
    ```

2.  **Navigate to the Server Directory:**
    ```bash
    cd server
    ```

3.  **Download Piper & Voice Model:**
    *   Go to the Piper Releases page and download the appropriate pre-compiled binary for your server's operating system (e.g., `piper_linux_x86_64.tar.gz`, `piper_windows_x64.zip`).
    *   Download a voice model (`.onnx` file) and its corresponding config file (`.onnx.json`) from the Piper samples page or Hugging Face.
    *   Create a subdirectory within `server`, for example, `server/piper_files/`.
    *   Extract the `piper` executable into `server/piper_files/`.
    *   Place the downloaded `.onnx` and `.onnx.json` files into `server/piper_files/`.

4.  **Configure Server Paths:**
    *   Open `server/piper_api.py` in a text editor.
    *   **CRITICAL STEP:** Modify the `PIPER_EXECUTABLE`, `MODEL_PATH`, and `CONFIG_PATH` variables near the top of the file. Make sure they point to the **exact locations** where you placed the `piper` executable, the `.onnx` model file, and the `.onnx.json` config file in the previous step.
      *Example:* If you put them in `server/piper_files/`, the paths might look like:
        ```python
        PIPER_EXECUTABLE = os.path.abspath("./piper_files/piper")
        MODEL_PATH = os.path.abspath("./piper_files/en_US-lessac-medium.onnx")
        CONFIG_PATH = os.path.abspath("./piper_files/en_US-lessac-medium.onnx.json")
        ```
        *(Adjust file names and paths based on your downloads and OS)*

5.  **Install Server Dependencies:**
    ```bash
    pip install Flask gunicorn requests
    ```

6.  **Run the Server:**
    *   **Recommended (for better performance):** Use Gunicorn. Adjust `--workers` based on your server's CPU cores (use `nproc` on Linux/macOS or check Task Manager on Windows to find the core count).
    ```bash
    # Example for a 4-core machine:
    gunicorn --workers 4 --bind 0.0.0.0:5100 --timeout 120 piper_api:app
    ```
    *   **Alternative (for simple testing):** Use the built-in Flask development server.
    ```bash
    python piper_api.py
    ```

7.  **Note the Server URL:** The server will now be listening for requests. Note down its URL.
    *   If running on your **local machine**, the URL is likely `http://127.0.0.1:5100` or `http://localhost:5100`.
    *   If running in a **GitHub Codespace**, the URL will be automatically generated and shown (usually like `https://<your-codespace-name>-5100.app.github.dev/`).
    *   If running on a **remote server/VM**, use its public or private IP address: `http://<server_ip_address>:5100`.

### Part 2: Setting up the GUI Client (`client` directory)

This is the graphical interface you'll use. Run these steps on the machine where you want to use the GUI (typically your main desktop/laptop).

1.  **Navigate to the Client Directory:**
    ```bash
    # From the repository root
    cd client
    ```

2.  **Install Client Dependencies:**
    ```bash
    pip install PySide6 requests
    ```

3.  **Run the GUI Client:**
    ```bash
    python piper_api_gui.py
    ```

4.  **Configure and Use the GUI:**
    *   The GUI window will appear.
    *   In the **"API Server URL"** field, enter the **full URL** of the running server that you noted in Server Step 7.
    *   Browse and select your input text file.
    *   Browse and select the desired output WAV file path.
    *   Click **"Synthesize via API"**.

## Security Note

The API server included in this project has **NO AUTHENTICATION OR SECURITY FEATURES**. It is designed for demonstration purposes or use within a trusted network.

**DO NOT expose the API server directly to the public internet** without implementing proper security measures (like API keys, authentication middleware, IP address restrictions, HTTPS, etc.). Doing so could allow anyone to use your server resources.

## Project Structure

*   `client/`: Contains the PySide6 GUI client code.
    *   `piper_api_gui.py`: The main GUI application script.
*   `server/`: Contains the Flask API server code.
    *   `piper_api.py`: The main Flask application.
    *   `piper_files/`: (Example location for Piper executable and models - **Not included in Git, create and populate manually as per setup instructions**).
*   `.gitignore`: Specifies files to be ignored by Git.
*   `README.md`: This file.