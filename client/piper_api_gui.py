import sys
import os
import subprocess
import threading
import queue
import datetime
import requests # Added for making HTTP requests
import wave     # Added for combining audio
import glob     # Added for finding temp files
import concurrent.futures # For parallel requests
import time     # For polling delay (though not used in this version)
import tempfile # Added for temporary file names

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                               QHBoxLayout, QGridLayout, QLabel, QPushButton,
                               QTextEdit, QFileDialog, QMessageBox, QLineEdit, QGroupBox, QProgressBar) # Added QProgressBar
from PySide6.QtGui import QIcon, QPixmap, QColor, QTextCharFormat, QTextCursor
from PySide6.QtCore import Qt, QThread, Signal, Slot, QSettings, QCoreApplication, QTime, QMetaObject, QBuffer, QIODevice


# --- Constants for Log Severity ---
INFO = 0
WARNING = 1
ERROR = 2
CRITICAL = 3

# --- Worker Thread for Processing Text Chunks via API (Parallel Chunks) ---
class ChunkProcessorWorker(QThread):
    # Signal to log messages to the GUI (message, severity)
    log_message = Signal(str, int)
    # Signal to indicate synthesis started
    synthesis_started = Signal()
    # Signal to indicate synthesis finished (successfully or with error)
    synthesis_finished = Signal()
    # Signal to update progress (current_step, total_steps, message)
    progress_update = Signal(int, int, str)
    # Signal to report the final output file path upon completion
    final_output_ready = Signal(str)
    # Signal to report the output file path upon completion (kept for potential future use)
    output_file_ready = Signal(str)
    # Signal to report errors that should trigger a message box
    critical_error = Signal(str)

    def __init__(self, api_url, text_to_speak, output_filepath):
        super().__init__()
        # Split text into smaller chunks (e.g., by sentence-like structures)
        # Replace newlines with spaces, then split by ". " - this is basic, might need refinement
        processed_text = text_to_speak.replace('\n', ' ').replace('\r', '')
        potential_chunks = processed_text.split('. ')
        self.text_chunks = []
        for i, chunk in enumerate(potential_chunks):
            trimmed_chunk = chunk.strip()
            if trimmed_chunk:
                # Add the period back if it wasn't the last chunk
                self.text_chunks.append(trimmed_chunk + "." if i < len(potential_chunks) - 1 else trimmed_chunk)

        self.api_url = api_url
        self.final_output_filepath = output_filepath
        self._is_running = False
        self._stop_requested = False
        self.temp_files = []
        self.max_workers = 12 # Number of parallel requests to send (adjust as needed) - TRY INCREASING THIS
        # Generate a unique base name for temporary files
        self.temp_base_name = os.path.join(tempfile.gettempdir(), f"piper_chunk_{os.path.basename(output_filepath)}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}")

    def run(self):
        """Processes text chunks in parallel, combines audio, and cleans up."""
        self._is_running = True
        self._stop_requested = False
        self.synthesis_started.emit()
        self.log_message.emit(f"Starting parallel chunked synthesis for {len(self.text_chunks)} chunks (max_workers={self.max_workers}).", INFO)

        total_chunks = len(self.text_chunks)
        # Use a dictionary to store results keyed by chunk index to maintain order
        results = {}
        chunks_processed = 0 # Counter for progress bar

        # Use ThreadPoolExecutor for parallel requests
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Dictionary to map futures to chunk index
            future_to_chunk = {}
            for i, chunk in enumerate(self.text_chunks):
                if self._stop_requested:
                    self.log_message.emit("Stop requested before submitting all chunks.", WARNING)
                    break

                chunk_num = i + 1
                temp_output_path = f"{self.temp_base_name}_{chunk_num:04d}.wav"
                # Submit the task to the executor
                future = executor.submit(self._synthesize_chunk, chunk_num, chunk, temp_output_path)
                future_to_chunk[future] = i # Store index

            # Process results as they complete
            for future in concurrent.futures.as_completed(future_to_chunk):
                if self._stop_requested:
                    # Attempt to cancel remaining futures (may not always work if already running)
                    self.log_message.emit("Stop requested, cancelling pending tasks...", WARNING)
                    # Don't break immediately, let already running tasks finish if possible,
                    # but don't process their results if they weren't successful.
                    # Mark stop flag so _synthesize_chunk knows.
                    continue # Continue loop to potentially catch errors from running tasks

                chunk_index = future_to_chunk[future]
                chunk_num = chunk_index + 1
                try:
                    temp_file_path = future.result() # Get the result (temp file path or None if failed/stopped)
                    if temp_file_path:
                        results[chunk_index] = temp_file_path # Store successful result with original index
                        chunks_processed += 1
                        progress_message = f"Completed chunk {chunks_processed}/{total_chunks}"
                        self.progress_update.emit(chunks_processed, total_chunks + 1, progress_message) # Update progress
                    elif not self._stop_requested: # Only raise critical error if stop wasn't requested
                        # Handle failure reported by _synthesize_chunk
                        self.log_message.emit(f"Chunk {chunk_num} failed (see previous errors). Aborting.", ERROR)
                        self.critical_error.emit(f"Synthesis failed on chunk {chunk_num}. Aborting.")
                        self._stop_requested = True # Signal other threads/tasks to stop
                        # Cancel remaining futures
                        executor.shutdown(wait=False, cancel_futures=True) # Python 3.9+
                        break # Exit result processing loop

                except concurrent.futures.CancelledError:
                     self.log_message.emit(f"Chunk {chunk_num} was cancelled.", WARNING)
                except Exception as exc:
                    self.log_message.emit(f'Chunk {chunk_num} generated an exception: {exc}', ERROR)
                    if not self._stop_requested: # Avoid duplicate critical errors
                         self.critical_error.emit(f"Error processing chunk {chunk_num}: {exc}")
                         self._stop_requested = True
                         executor.shutdown(wait=False, cancel_futures=True) # Python 3.9+
                    break # Exit result processing loop

        # --- Post-Processing ---
        # Collect temp files that were actually created before combining/cleanup
        created_temp_files = list(results.values())

        # Check if processing was stopped or failed critically
        if self._stop_requested or len(results) < total_chunks:
             self.log_message.emit("Synthesis aborted or failed before completion.", WARNING)
             self._cleanup_temp_files(created_temp_files) # Clean up any files that were created
             self._is_running = False
             self.synthesis_finished.emit()
             return

        # --- Step 2: Combine Audio Files ---
        # Ensure temp files are in the correct order based on the original chunk index
        self.temp_files = [results[i] for i in sorted(results.keys())]

        self.progress_update.emit(total_chunks + 1, total_chunks + 1, "Combining audio chunks...")
        self.log_message.emit("All chunks synthesized successfully. Combining audio...", INFO)

        # Ensure final output directory exists
        output_dir = os.path.dirname(self.final_output_filepath)
        if output_dir and not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir)
                self.log_message.emit(f"Created output directory: {output_dir}", INFO)
            except Exception as e:
                error_msg = f"Error creating final output directory {output_dir}: {e}"
                self.log_message.emit(error_msg, CRITICAL)
                self.critical_error.emit(error_msg)
                self._cleanup_temp_files(self.temp_files)
                self._is_running = False
                self.synthesis_finished.emit()
                return

        try:
            self._combine_wav_files(self.temp_files, self.final_output_filepath)
            self.log_message.emit(f"Successfully combined audio to {self.final_output_filepath}", INFO)
            self.final_output_ready.emit(self.final_output_filepath) # Emit final path
        except Exception as e:
            error_msg = f"Error combining audio files: {e}"
            self.log_message.emit(error_msg, CRITICAL)
            self.critical_error.emit(error_msg)
        finally:
            # --- Step 3: Cleanup ---
            self._cleanup_temp_files(self.temp_files)
            self._is_running = False
            self.synthesis_finished.emit()

    def _synthesize_chunk(self, chunk_num, chunk_text, temp_output_path):
        """Sends a single chunk to the API and saves the result. Runs in a worker thread."""
        # Check stop flag at the beginning
        if self._stop_requested:
            self.log_message.emit(f"Skipping chunk {chunk_num} due to stop request.", WARNING)
            return None

        synthesize_url = f"{self.api_url.rstrip('/')}/synthesize"
        payload = {"text": chunk_text}
        headers = {"Content-Type": "application/json"}

        self.log_message.emit(f"Starting request for chunk {chunk_num}...", INFO)

        try:
            # Use a reasonable timeout per chunk (e.g., 60s, less than the suspected proxy timeout)
            response = requests.post(synthesize_url, json=payload, headers=headers, stream=True, timeout=60)

            # Check stop flag again after request returns but before processing
            if self._stop_requested:
                self.log_message.emit(f"Ignoring response for chunk {chunk_num} due to stop request.", WARNING)
                return None

            if response.status_code == 200:
                try:
                    with open(temp_output_path, 'wb') as f:
                        # Check stop flag less frequently during download
                        for i, audio_chunk in enumerate(response.iter_content(chunk_size=8192)):
                            # Check stop flag periodically during download
                            if i % 10 == 0 and self._stop_requested: # Check every ~80KB
                                self.log_message.emit(f"Stopping download for chunk {chunk_num}...", WARNING)
                                break
                            f.write(audio_chunk)

                    # If loop was broken by stop request, clean up and return None
                    if self._stop_requested:
                         if os.path.exists(temp_output_path):
                              try:
                                   os.remove(temp_output_path)
                              except OSError as e:
                                   self.log_message.emit(f"Error removing partial file for chunk {chunk_num}: {e}", ERROR)
                         self.log_message.emit(f"Download stopped for chunk {chunk_num}.", WARNING)
                         return None

                    # Check if file is valid after writing
                    if os.path.exists(temp_output_path) and os.path.getsize(temp_output_path) > 0:
                        self.log_message.emit(f"Chunk {chunk_num} saved successfully.", INFO)
                        return temp_output_path # Return path on success
                    else:
                        self.log_message.emit(f"Chunk {chunk_num} resulted in empty/missing file.", ERROR)
                        if os.path.exists(temp_output_path): os.remove(temp_output_path) # Clean up empty file
                        return None # Indicate failure

                except Exception as e:
                    error_msg = f"Error saving audio for chunk {chunk_num}: {e}"
                    self.log_message.emit(error_msg, ERROR)
                    return None # Indicate failure
            else:
                # Handle API error for the chunk
                try: error_detail = response.json().get('error', response.text[:200])
                except: error_detail = response.text[:200]
                error_msg = f"API Error for chunk {chunk_num}: Status {response.status_code}. Detail: {error_detail}"
                self.log_message.emit(error_msg, ERROR)
                return None # Indicate failure

        except requests.exceptions.Timeout:
            error_msg = f"API call timed out for chunk {chunk_num} (timeout=60s)."
            self.log_message.emit(error_msg, ERROR)
            return None # Indicate failure
        except requests.exceptions.RequestException as e:
            # Check if stop was requested during a network error
            if self._stop_requested:
                 self.log_message.emit(f"Network error for chunk {chunk_num} ignored due to stop request.", WARNING)
                 return None
            error_msg = f"Network error during chunk {chunk_num}: {e}"
            self.log_message.emit(error_msg, ERROR)
            return None # Indicate failure
        except Exception as e:
            # Check if stop was requested during an unexpected error
            if self._stop_requested:
                 self.log_message.emit(f"Unexpected error for chunk {chunk_num} ignored due to stop request.", WARNING)
                 return None
            error_msg = f"Unexpected error processing chunk {chunk_num}: {e}"
            self.log_message.emit(error_msg, ERROR)
            # Don't emit critical error here, let the main loop handle it based on return value
            return None # Indicate failure


    def _combine_wav_files(self, input_files, output_file):
        """Combines multiple WAV files into one."""
        if not input_files:
            raise ValueError("No input files provided for combining.")

        outfile = None
        valid_input_files = [f for f in input_files if os.path.exists(f) and os.path.getsize(f) > 0]
        if not valid_input_files:
             raise ValueError("No valid input audio files found to combine.")

        self.log_message.emit(f"Combining {len(valid_input_files)} valid audio chunks...", INFO)

        try:
            # Open the first valid file to get parameters
            with wave.open(valid_input_files[0], 'rb') as infile:
                params = infile.getparams()

            # Open the output file with the same parameters
            outfile = wave.open(output_file, 'wb')
            outfile.setparams(params)

            # Write data from each valid input file
            for filename in valid_input_files:
                with wave.open(filename, 'rb') as infile:
                    # Ensure parameters match (optional, but good practice)
                    if infile.getparams() != params:
                        self.log_message.emit(f"Warning: Parameters mismatch in {filename}", WARNING)
                        # Handle mismatch if necessary (e.g., skip file, try to convert)
                        # For now, we'll assume Piper output is consistent
                    frames = infile.readframes(infile.getnframes())
                    outfile.writeframes(frames)
        finally:
            if outfile:
                outfile.close()

    def _cleanup_temp_files(self, files_to_delete):
        """Deletes the temporary WAV files."""
        if not files_to_delete: return
        self.log_message.emit(f"Cleaning up {len(files_to_delete)} temporary files...", INFO)
        deleted_count = 0
        for temp_file in files_to_delete:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                    deleted_count += 1
            except Exception as e:
                self.log_message.emit(f"Error deleting temporary file {temp_file}: {e}", ERROR)
        self.log_message.emit(f"Deleted {deleted_count} temporary files.", INFO)

    @Slot()
    def stop(self):
        """Slot to request stopping the chunk processing."""
        if self._is_running:
            self.log_message.emit("Stop signal received. Requesting stop...", INFO)
            self._stop_requested = True # Worker checks this flag


# --- Main Application Window ---

class PiperAPIGUIWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Piper TTS API Client")
        # Adjust geometry for a slightly less wide window now
        self.setGeometry(100, 100, 1000, 600)

        # Use different settings group to avoid conflicts with the original GUI
        QCoreApplication.setOrganizationName("YourCompanyName")
        QCoreApplication.setApplicationName("PiperTTSAPIClient")
        self.settings = QSettings()

        self.worker_thread = None
        self.worker = None
        self.current_text_file_path = None
        self.current_output_file_path = None

        # Define colors for log levels
        self.log_colors = {
            INFO: QColor("#cccccc"),     # Light gray for info
            WARNING: QColor("#ffcc00"),  # Yellow/Orange for warnings
            ERROR: QColor("#ff6600"),    # Orange/Red for errors
            CRITICAL: QColor("#ff3300")  # Bright Red for critical errors
        }


        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        # Use a main vertical layout for the whole window content
        main_vertical_layout = QVBoxLayout(central_widget)

        # --- Top Horizontal Layout for Paths and Text ---
        top_horizontal_layout = QHBoxLayout()

        # --- File and Path Selection Group Box (Left) ---
        paths_group_box = QGroupBox("File and API Selection")
        paths_layout = QGridLayout(paths_group_box)

        # Text File Selection
        paths_layout.addWidget(QLabel("Text File:"), 0, 0, alignment=Qt.AlignRight)
        self.text_file_path_entry = QLineEdit()
        self.text_file_path_entry.setReadOnly(True)
        paths_layout.addWidget(self.text_file_path_entry, 0, 1)
        self.browse_text_button = QPushButton("Browse")
        self.browse_text_button.clicked.connect(self.browse_text_file)
        paths_layout.addWidget(self.browse_text_button, 0, 2)

        # API Server URL Input
        paths_layout.addWidget(QLabel("API Server URL:"), 1, 0, alignment=Qt.AlignRight)
        self.api_url_entry = QLineEdit()
        self.api_url_entry.setPlaceholderText("e.g., http://localhost:5100")
        self.api_url_entry.textChanged.connect(self.update_synthesize_button_state) # Update button when URL changes
        paths_layout.addWidget(self.api_url_entry, 1, 1, 1, 2) # Span across 2 columns

        # Output Audio File Path
        paths_layout.addWidget(QLabel("Output Audio File:"), 2, 0, alignment=Qt.AlignRight)
        self.output_file_path_entry = QLineEdit()
        self.output_file_path_entry.setReadOnly(True) # User browses, doesn't type
        paths_layout.addWidget(self.output_file_path_entry, 2, 1)
        self.browse_output_button = QPushButton("Browse")
        self.browse_output_button.clicked.connect(self.browse_output_file)
        paths_layout.addWidget(self.browse_output_button, 2, 2)

        # Add the paths group box to the top horizontal layout (left side)
        # Set a stretch factor to control width distribution (e.g., 1 for paths, 2 for text)
        top_horizontal_layout.addWidget(paths_group_box, 1)


        # --- Text Display Area (Right) ---
        text_group_box = QGroupBox("Text to Synthesize")
        text_layout = QVBoxLayout(text_group_box)
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        text_layout.addWidget(self.text_edit)
        # Add the text group box to the top horizontal layout (right side)
        top_horizontal_layout.addWidget(text_group_box, 2) # Give text area more horizontal space


        # Add the top horizontal layout to the main vertical layout
        main_vertical_layout.addLayout(top_horizontal_layout, 1) # Give top section stretch factor


        # --- Control Buttons (Below) ---
        control_layout = QHBoxLayout()
        self.synthesize_button = QPushButton("Synthesize via API")
        self.synthesize_button.clicked.connect(self.start_synthesis)
        self.synthesize_button.setEnabled(False)
        control_layout.addWidget(self.synthesize_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_synthesis)
        self.stop_button.setEnabled(False)
        control_layout.addWidget(self.stop_button)

        control_layout.addStretch(1)

        # Add the control buttons layout to the main vertical layout
        main_vertical_layout.addLayout(control_layout)

        # --- Progress Bar ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100) # Default range, will be updated
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        main_vertical_layout.addWidget(self.progress_bar)

        # --- Status Log (Bottom) ---
        log_group_box = QGroupBox("Status Log")
        log_layout = QVBoxLayout(log_group_box)
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setFixedHeight(150) # Keep fixed height for log
        self.status_text.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        log_layout.addWidget(self.status_text)
        # Add the status log group box to the main vertical layout
        main_vertical_layout.addWidget(log_group_box)


        # --- Apply Basic Dark Theme Styling ---
        try:
            self.setStyleSheet("""
                QMainWindow {
                    background-color: #2b2b2b;
                    color: #cccccc;
                    font-family: "Segoe UI", "Helvetica Neue", "Arial", sans-serif;
                    font-size: 10pt;
                }
                QGroupBox {
                    border: 1px solid #454545;
                    border-radius: 5px;
                    margin-top: 18px;
                    padding-top: 15px;
                    padding-left: 10px;
                    padding-right: 10px;
                    color: #cccccc;
                    font-weight: bold;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    subcontrol-position: top left;
                    padding: 0 3px;
                    left: 10px;
                    color: #cccccc;
                    background-color: #2b2b2b;
                }
                QLabel {
                    color: #cccccc;
                }
                QLineEdit {
                    background-color: #3c3c3c;
                    color: #cccccc;
                    padding: 6px;
                    border: 1px solid #454545;
                    border-radius: 4px;
                    selection-background-color: #0078d4;
                    selection-color: white;
                }
                 QLineEdit:readOnly {
                     background-color: #333333;
                     color: #a0a0a0;
                 }
                QPushButton {
                    background-color: #505050;
                    color: #cccccc;
                    border: none;
                    padding: 8px 15px;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #606060;
                }
                QPushButton:pressed {
                    background-color: #707070;
                }
                QPushButton:disabled {
                    background-color: #404040;
                    color: #707070;
                }
                 QPushButton#synthesizeButton {
                     background-color: #5cb85c; /* Green */
                     color: white;
                     font-weight: bold;
                 }
                 QPushButton#synthesizeButton:hover {
                     background-color: #4cae4c;
                 }
                 QPushButton#synthesizeButton:pressed {
                     background-color: #398439;
                 }
                 QPushButton#stopButton {
                     background-color: #d9534f; /* Red */
                     color: white;
                     font-weight: bold;
                 }
                 QPushButton#stopButton:hover {
                     background-color: #c9302c;
                 }
                 QPushButton#stopButton:pressed {
                     background-color: #ac2925;
                 }
                 QTextEdit {
                    background-color: #3c3c3c;
                    color: #cccccc; /* Default text color, will be overridden by format */
                    border: 1px solid #454545;
                    border-radius: 4px;
                    padding: 5px;
                }
                 QProgressBar {
                    border: 1px solid #454545;
                    border-radius: 5px;
                    text-align: center;
                    height: 20px;
                    color: #cccccc;
                    background-color: #3c3c3c;
                }
                 QProgressBar::chunk {
                     background-color: #007ACC;
                     border-radius: 4px;
                 }
                 QMessageBox {
                    background-color: #2b2b2b;
                    color: #cccccc;
                }
                QMessageBox QPushButton {
                    background-color: #505050;
                    color: #cccccc;
                    border: 1px solid #454545;
                    padding: 5px 10px;
                    border-radius: 3px;
                }
                 QMessageBox QPushButton:hover {
                     background-color: #606060;
                 }
                 QMessageBox QPushButton:pressed {
                     background-color: #707070;
                 }
            """)
        except Exception as e:
            print(f"Error applying styling: {e}")


        # Set object names for specific styling
        self.synthesize_button.setObjectName("synthesizeButton")
        self.stop_button.setObjectName("stopButton")

        # Load saved settings on startup
        self.load_settings()
        # Update button state based on loaded settings
        self.update_synthesize_button_state()


    def load_settings(self):
        """Loads saved paths from QSettings."""
        self.settings.beginGroup("PiperTTSAPIPaths") # Use different group name
        self.text_file_path_entry.setText(self.settings.value("text_file_path", ""))
        # Load API URL, default to placeholder if empty or not found
        saved_api_url = self.settings.value("api_url", "")
        default_api_url = "http://localhost:5100" # Placeholder
        self.api_url_entry.setText(saved_api_url if saved_api_url else default_api_url)
        self.output_file_path_entry.setText(self.settings.value("output_file_path", ""))
        self.settings.endGroup()

        # Load text file content if path is saved and file exists
        saved_text_file = self.text_file_path_entry.text()
        if saved_text_file and os.path.exists(saved_text_file):
            try:
                with open(saved_text_file, 'r', encoding='utf-8') as f:
                    self.text_edit.setText(f.read())
                    self.current_text_file_path = saved_text_file
                    self.log_message(f"Loaded text from saved file: {saved_text_file}", INFO)
            except Exception as e:
                self.log_message(f"Error loading saved text file {saved_text_file}: {e}", ERROR)
                self.text_edit.clear()
                self.current_text_file_path = None
        else:
             self.text_edit.clear()
             self.current_text_file_path = None


    def save_settings(self):
        """Saves current paths to QSettings."""
        self.settings.beginGroup("PiperTTSAPIPaths") # Use different group name
        self.settings.setValue("text_file_path", self.text_file_path_entry.text())
        self.settings.setValue("api_url", self.api_url_entry.text()) # Save API URL
        self.settings.setValue("output_file_path", self.output_file_path_entry.text())
        self.settings.endGroup()
        self.settings.sync()


    def closeEvent(self, event):
        """Handles window closing and saves settings."""
        self.save_settings()
        if self.worker_thread is not None and self.worker_thread.isRunning():
            reply = QMessageBox.question(self, "Quit", "Synthesis is in progress. Do you want to stop and quit?",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.stop_synthesis()
                self.worker_thread.wait(5000) # Give thread a chance to finish
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


    @Slot()
    def browse_text_file(self):
        """Opens a file dialog to select a text file."""
        filepath, _ = QFileDialog.getOpenFileName(self, "Select Text File", "", "Text Files (*.txt);;All Files (*)")
        if filepath:
            self.text_file_path_entry.setText(filepath)
            self.current_text_file_path = filepath
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    self.text_edit.setText(f.read())
                    self.log_message("Text file loaded successfully.", INFO)
            except Exception as e:
                self.text_edit.clear()
                self.current_text_file_path = None
                self.log_message(f"Error loading text file: {e}", ERROR)
                QMessageBox.critical(self, "File Error", f"Could not load text file:\n{e}")
            self.update_synthesize_button_state()
            self.save_settings() # Save path immediately


    @Slot()
    def browse_output_file(self):
        """Opens a save file dialog for the output audio file."""
        filepath, _ = QFileDialog.getSaveFileName(self, "Save Audio File", "", "WAV Audio Files (*.wav);;All Files (*)")
        if filepath:
            if not filepath.lower().endswith('.wav'):
                filepath += '.wav'
            self.output_file_path_entry.setText(filepath)
            self.current_output_file_path = filepath
            self.update_synthesize_button_state()
            self.save_settings() # Save path immediately


    def update_synthesize_button_state(self):
        """Enables or disables the synthesize button based on required inputs."""
        is_ready = (
            os.path.exists(self.text_file_path_entry.text()) and # Check if text file exists
            self.api_url_entry.text().strip().startswith("http") and # Basic check for API URL
            self.output_file_path_entry.text().strip() != "" and
            self.text_edit.toPlainText().strip() != ""
        )
        self.synthesize_button.setEnabled(is_ready)


    @Slot()
    def start_synthesis(self):
        """Initiates the API synthesis process in a worker thread."""
        if self.worker_thread is not None and self.worker_thread.isRunning():
            self.log_message("Synthesis is already running.", WARNING)
            return

        text_to_speak = self.text_edit.toPlainText()
        api_url = self.api_url_entry.text().strip()
        output_filepath = self.output_file_path_entry.text()

        # Final validation before starting
        if not api_url.startswith("http"):
             self.log_message(f"Invalid API URL: {api_url}", ERROR)
             QMessageBox.critical(self, "Input Error", f"Please enter a valid API Server URL (starting with http or https):\n{api_url}")
             return
        if not text_to_speak.strip():
              self.log_message("No text loaded for synthesis.", WARNING)
              QMessageBox.warning(self, "Input Error", "Please load a text file with content.")
              return
        if not output_filepath.strip():
             self.log_message("No output file path specified.", WARNING)
             QMessageBox.warning(self, "Input Error", "Please specify an output audio file path.")
             return


        self.worker = ChunkProcessorWorker(api_url, text_to_speak, output_filepath) # Use ChunkProcessorWorker
        self.worker_thread = QThread()

        self.worker.moveToThread(self.worker_thread)

        # Connect signals and slots
        self.worker.log_message.connect(self.log_message)
        self.worker.synthesis_started.connect(self.on_synthesis_started)
        self.worker.synthesis_finished.connect(self.on_synthesis_finished)
        self.worker.progress_update.connect(self.on_progress_update) # Connect progress signal
        self.worker.final_output_ready.connect(self.on_final_output_ready) # Connect final output signal
        self.worker.critical_error.connect(self.handle_critical_error)


        self.worker_thread.started.connect(self.worker.run)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        # Ensure worker is deleted after finishing to prevent memory leaks
        self.worker.synthesis_finished.connect(self.worker.deleteLater)

        # Start the thread
        self.worker_thread.start()

    @Slot()
    def stop_synthesis(self):
        """Stops the ongoing synthesis process."""
        if self.worker is not None and self.worker_thread is not None and self.worker_thread.isRunning():
            self.worker.stop()


    @Slot()
    def on_synthesis_started(self):
        """Updates GUI when synthesis starts."""
        self.synthesize_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.set_input_paths_enabled(False) # Disable browsing while synthesizing
        self.progress_bar.setValue(0) # Reset progress bar


    @Slot()
    def on_synthesis_finished(self):
        """Updates GUI when synthesis finishes."""
        # Re-enable button only if state is still valid
        self.update_synthesize_button_state()
        self.stop_button.setEnabled(False)
        self.set_input_paths_enabled(True) # Re-enable browsing
        # Optionally reset progress bar text or value here if desired
        self.progress_bar.setFormat("Ready") # Reset progress bar text
        self.progress_bar.setValue(0)


        # Clean up thread reference
        if self.worker_thread and not self.worker_thread.isRunning():
             self.worker_thread = None
             self.worker = None


    @Slot(int, int, str)
    def on_progress_update(self, value, max_value, message):
        """Updates the progress bar."""
        if max_value > 0:
            self.progress_bar.setRange(0, max_value)
            self.progress_bar.setValue(value)
            self.progress_bar.setFormat(f"{message} ({value}/{max_value})") # Show count
        else:
            self.progress_bar.setRange(0, 0) # Indeterminate if no steps
            self.progress_bar.setFormat(message)

    @Slot(str)
    def on_final_output_ready(self, filepath):
        """Handles the signal when the final combined output file is ready."""
        self.log_message(f"Final audio file saved to: {filepath}", INFO)


    @Slot(str, int)
    def log_message(self, message, severity=INFO):
        """Appends a message to the status log with color formatting."""
        timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        formatted_message = f"{timestamp} {message}\n"

        cursor = self.status_text.textCursor()
        cursor.movePosition(QTextCursor.End)

        format = QTextCharFormat()
        color = self.log_colors.get(severity, self.log_colors[INFO])
        format.setForeground(color)

        cursor.insertText(formatted_message, format)
        self.status_text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.status_text.ensureCursorVisible()


    @Slot(str)
    def handle_critical_error(self, error_message):
        """Handles critical errors reported by the worker."""
        # Log message is already emitted by the worker or calling function
        QMessageBox.critical(self, "Critical Error", error_message)


    def set_input_paths_enabled(self, enabled):
        """Enables or disables the input path selection widgets."""
        self.api_url_entry.setEnabled(enabled) # Enable/disable API URL field
        self.browse_text_button.setEnabled(enabled)
        self.browse_output_button.setEnabled(enabled)


if __name__ == "__main__":
    if not QApplication.instance():
        app = QApplication(sys.argv)
    else:
        app = QApplication.instance()

    # Use different settings group to avoid conflicts with the original GUI
    QCoreApplication.setOrganizationName("YourCompanyName")
    QCoreApplication.setApplicationName("PiperTTSAPIClient")

    main_window = PiperAPIGUIWindow()
    main_window.show()
    sys.exit(app.exec())