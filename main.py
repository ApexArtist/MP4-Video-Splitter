import sys
import os
import subprocess
import shutil
import datetime
import psutil
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QTextEdit, QComboBox,
    QCheckBox, QSpinBox, QGroupBox, QGridLayout, QSplitter, QFrame
)
from PySide6.QtCore import QThread, Signal, QTimer, Qt
from PySide6.QtGui import QFont, QIcon, QPalette, QColor
import traceback


class FFmpegValidator:
    """FFmpeg validation and management"""
    
    @staticmethod
    def check_ffmpeg():
        """Check if FFmpeg is available and working"""
        try:
            ffmpeg_path = shutil.which('ffmpeg')
            if not ffmpeg_path:
                return False, "FFmpeg not found in system PATH", None
            
            result = subprocess.run(['ffmpeg', '-version'], 
                                  capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                version_line = result.stdout.split('\n')[0]
                return True, f"FFmpeg found: {version_line}", ffmpeg_path
            else:
                return False, f"FFmpeg test failed: {result.stderr}", None
        except subprocess.TimeoutExpired:
            return False, "FFmpeg check timed out", None
        except Exception as e:
            return False, f"FFmpeg check error: {str(e)}", None

    @staticmethod
    def get_video_info(file_path):
        """Get video information using FFprobe"""
        try:
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', file_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                import json
                data = json.loads(result.stdout)
                
                # Extract video info
                format_info = data.get('format', {})
                duration = float(format_info.get('duration', 0))
                size = int(format_info.get('size', 0))
                
                # Find video stream
                video_stream = None
                audio_stream = None
                for stream in data.get('streams', []):
                    if stream.get('codec_type') == 'video' and not video_stream:
                        video_stream = stream
                    elif stream.get('codec_type') == 'audio' and not audio_stream:
                        audio_stream = stream
                
                return {
                    'duration': duration,
                    'size': size,
                    'video_codec': video_stream.get('codec_name') if video_stream else 'unknown',
                    'audio_codec': audio_stream.get('codec_name') if audio_stream else 'none',
                    'width': video_stream.get('width') if video_stream else 0,
                    'height': video_stream.get('height') if video_stream else 0,
                    'fps': eval(video_stream.get('r_frame_rate', '0/1')) if video_stream else 0
                }
            else:
                return None
        except Exception as e:
            print(f"Error getting video info: {e}")
            return None


class SplitWorker(QThread):
    """Enhanced worker thread for video splitting"""
    
    progress = Signal(int)
    finished = Signal(str, int)  # output_dir, clips_created
    error = Signal(str)
    status_update = Signal(str)
    clip_completed = Signal(int, str)  # clip_number, file_path

    def __init__(self, input_file, clip_duration, num_clips, output_dir, 
                 overlap=0, use_copy=True, quality='medium'):
        super().__init__()
        self.input_file = input_file
        self.clip_duration = clip_duration
        self.num_clips = num_clips
        self.output_dir = output_dir
        self.overlap = overlap
        self.use_copy = use_copy
        self.quality = quality
        self.should_stop = False

    def stop(self):
        self.should_stop = True

    def run(self):
        try:
            # Validate FFmpeg
            ffmpeg_ok, ffmpeg_msg, ffmpeg_path = FFmpegValidator.check_ffmpeg()
            if not ffmpeg_ok:
                self.error.emit(f"FFmpeg validation failed: {ffmpeg_msg}")
                return

            self.status_update.emit("Analyzing video file...")
            
            # Get video information
            video_info = FFmpegValidator.get_video_info(self.input_file)
            if not video_info:
                self.error.emit("Failed to analyze video file. File may be corrupted or unsupported.")
                return

            total_duration = video_info['duration']
            if total_duration <= 0:
                self.error.emit("Invalid video duration detected.")
                return

            self.status_update.emit(f"Video: {total_duration:.1f}s, {video_info['width']}x{video_info['height']}, "
                                  f"{video_info['video_codec']}/{video_info['audio_codec']}")

            # Create output directory
            os.makedirs(self.output_dir, exist_ok=True)

            # Calculate clips
            if self.overlap > 0:
                # With overlap, clips can extend beyond simple division
                actual_clips = min(self.num_clips, 
                                 int((total_duration + self.overlap) / (self.clip_duration - self.overlap)))
            else:
                max_possible_clips = int(total_duration / self.clip_duration)
                if total_duration % self.clip_duration > 0:
                    max_possible_clips += 1
                actual_clips = min(self.num_clips, max_possible_clips)

            if actual_clips == 0:
                self.error.emit(f"Video too short. Duration: {total_duration:.1f}s, Required: {self.clip_duration}s")
                return

            self.status_update.emit(f"Creating {actual_clips} clips...")

            clips_created = 0
            for i in range(actual_clips):
                if self.should_stop:
                    self.status_update.emit("Stopping...")
                    break

                # Calculate timing
                if self.overlap > 0:
                    start = max(0, i * (self.clip_duration - self.overlap))
                else:
                    start = i * self.clip_duration
                
                end = min(start + self.clip_duration, total_duration)
                
                if start >= total_duration:
                    break

                clip_duration_actual = end - start
                if clip_duration_actual < 1.0:  # Skip clips shorter than 1 second
                    continue

                output_path = os.path.join(self.output_dir, f"clip_{i+1:03d}.mp4")
                
                self.status_update.emit(f"Processing clip {i+1}/{actual_clips} "
                                      f"({start:.1f}s - {end:.1f}s)")

                try:
                    # Build FFmpeg command
                    if self.use_copy:
                        # Stream copy - fastest, no quality loss
                        cmd = [
                            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                            '-ss', str(start),
                            '-i', self.input_file,
                            '-t', str(clip_duration_actual),
                            '-c', 'copy',
                            '-avoid_negative_ts', 'make_zero',
                            '-movflags', '+faststart',
                            output_path
                        ]
                    else:
                        # Re-encode with quality settings
                        quality_settings = {
                            'fast': ['-preset', 'ultrafast', '-crf', '28'],
                            'medium': ['-preset', 'medium', '-crf', '23'],
                            'high': ['-preset', 'slow', '-crf', '18']
                        }
                        
                        cmd = [
                            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                            '-ss', str(start),
                            '-i', self.input_file,
                            '-t', str(clip_duration_actual),
                            '-c:v', 'libx264',
                            '-c:a', 'aac',
                            '-b:a', '128k'
                        ] + quality_settings.get(self.quality, quality_settings['medium']) + [
                            '-movflags', '+faststart',
                            output_path
                        ]

                    # Execute FFmpeg
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    
                    if result.returncode != 0:
                        raise Exception(f"FFmpeg error: {result.stderr}")

                    # Verify output file
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                        clips_created += 1
                        file_size = os.path.getsize(output_path) / (1024 * 1024)  # MB
                        self.clip_completed.emit(i + 1, f"{output_path} ({file_size:.1f}MB)")
                    else:
                        raise Exception("Output file is missing or too small")

                except subprocess.TimeoutExpired:
                    self.error.emit(f"Timeout processing clip {i+1}")
                    break
                except Exception as e:
                    self.error.emit(f"Failed to create clip {i+1}: {str(e)}")
                    break

                # Update progress
                progress_percent = int(((i + 1) / actual_clips) * 100)
                self.progress.emit(progress_percent)

            if not self.should_stop and clips_created > 0:
                self.status_update.emit(f"✅ Successfully created {clips_created} clips!")
                self.finished.emit(self.output_dir, clips_created)
            elif clips_created == 0:
                self.error.emit("No clips were created successfully.")
            else:
                self.status_update.emit(f"⚠️ Operation stopped. {clips_created} clips created.")

        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
            self.error.emit(error_msg)


class ModernButton(QPushButton):
    """Styled button for modern look"""
    
    def __init__(self, text, color="#4CAF50"):
        super().__init__(text)
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {color};
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
                min-width: 120px;
            }}
            QPushButton:hover {{
                background-color: {self.adjust_color(color, -20)};
            }}
            QPushButton:pressed {{
                background-color: {self.adjust_color(color, -40)};
            }}
            QPushButton:disabled {{
                background-color: #cccccc;
                color: #666666;
            }}
        """)

    def adjust_color(self, color, amount):
        """Adjust color brightness"""
        color = color.lstrip('#')
        rgb = [int(color[i:i+2], 16) for i in (0, 2, 4)]
        rgb = [max(0, min(255, c + amount)) for c in rgb]
        return f"#{''.join(f'{c:02x}' for c in rgb)}"


class VideoSplitterApp(QWidget):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        self.init_variables()
        self.init_ui()
        self.check_system_requirements()

    def init_variables(self):
        """Initialize application variables"""
        self.input_file = None
        self.output_dir = None
        self.worker = None
        self.video_info = None
        
        # UI update timer
        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self.update_ui)

    def init_ui(self):
        """Initialize user interface"""
        self.setWindowTitle("MP4 Video Splitter Pro - v3.0")
        self.setMinimumSize(800, 700)
        
        # Apply modern styling
        self.setStyleSheet("""
            QWidget {
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 10pt;
            }
            QGroupBox {
                font-weight: bold;
                border: 2px solid #cccccc;
                border-radius: 8px;
                margin-top: 1ex;
                padding-top: 5px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
            QLineEdit, QComboBox, QSpinBox {
                padding: 6px;
                border: 1px solid #ddd;
                border-radius: 4px;
                background-color: white;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
                border: 2px solid #4CAF50;
            }
            QTextEdit {
                border: 1px solid #ddd;
                border-radius: 4px;
                background-color: #fafafa;
            }
            QProgressBar {
                border: 1px solid #ddd;
                border-radius: 4px;
                text-align: center;
                background-color: #f0f0f0;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                border-radius: 3px;
            }
        """)

        # Main layout
        main_layout = QVBoxLayout()
        
        # Create splitter for resizable sections
        splitter = QSplitter(Qt.Vertical)
        
        # Top section - Controls
        controls_widget = self.create_controls_section()
        splitter.addWidget(controls_widget)
        
        # Bottom section - Progress and logs
        progress_widget = self.create_progress_section()
        splitter.addWidget(progress_widget)
        
        # Set splitter proportions
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        
        main_layout.addWidget(splitter)
        self.setLayout(main_layout)

    def create_controls_section(self):
        """Create the controls section"""
        widget = QWidget()
        layout = QVBoxLayout()

        # File Selection Group
        file_group = QGroupBox("📁 File Selection")
        file_layout = QGridLayout()

        # Video file selection
        self.file_label = QLabel("No video file selected")
        self.file_label.setStyleSheet("color: #666; font-style: italic;")
        self.select_file_btn = ModernButton("Select Video File", "#2196F3")
        self.select_file_btn.clicked.connect(self.select_video_file)

        # Export folder selection
        self.export_label = QLabel("No export folder selected")
        self.export_label.setStyleSheet("color: #666; font-style: italic;")
        self.select_folder_btn = ModernButton("Select Export Folder", "#FF9800")
        self.select_folder_btn.clicked.connect(self.select_export_folder)

        # Video info display
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #333; font-size: 9pt;")

        file_layout.addWidget(QLabel("Video File:"), 0, 0)
        file_layout.addWidget(self.file_label, 0, 1)
        file_layout.addWidget(self.select_file_btn, 0, 2)
        file_layout.addWidget(QLabel("Export Folder:"), 1, 0)
        file_layout.addWidget(self.export_label, 1, 1)
        file_layout.addWidget(self.select_folder_btn, 1, 2)
        file_layout.addWidget(self.info_label, 2, 0, 1, 3)

        file_group.setLayout(file_layout)

        # Settings Group
        settings_group = QGroupBox("⚙️ Split Settings")
        settings_layout = QGridLayout()

        # Clip duration
        settings_layout.addWidget(QLabel("Clip Duration (seconds):"), 0, 0)
        self.duration_input = QSpinBox()
        self.duration_input.setRange(1, 3600)
        self.duration_input.setValue(30)
        self.duration_input.setSuffix(" seconds")
        settings_layout.addWidget(self.duration_input, 0, 1)

        # Number of clips
        settings_layout.addWidget(QLabel("Number of Clips:"), 0, 2)
        self.num_clips_input = QSpinBox()
        self.num_clips_input.setRange(1, 1000)
        self.num_clips_input.setValue(10)
        settings_layout.addWidget(self.num_clips_input, 0, 3)

        # Overlap
        settings_layout.addWidget(QLabel("Overlap (seconds):"), 1, 0)
        self.overlap_input = QSpinBox()
        self.overlap_input.setRange(0, 60)
        self.overlap_input.setValue(0)
        self.overlap_input.setSuffix(" seconds")
        settings_layout.addWidget(self.overlap_input, 1, 1)

        # Processing mode
        settings_layout.addWidget(QLabel("Processing Mode:"), 1, 2)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Stream Copy (Fast)", "Re-encode (Slow)"])
        self.mode_combo.setCurrentIndex(0)
        settings_layout.addWidget(self.mode_combo, 1, 3)

        # Quality setting (only for re-encode)
        settings_layout.addWidget(QLabel("Quality (Re-encode):"), 2, 0)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["Fast", "Medium", "High"])
        self.quality_combo.setCurrentIndex(1)
        settings_layout.addWidget(self.quality_combo, 2, 1)

        settings_group.setLayout(settings_layout)

        # Action buttons
        button_layout = QHBoxLayout()
        
        self.start_btn = ModernButton("🚀 Start Splitting", "#4CAF50")
        self.start_btn.clicked.connect(self.start_splitting)
        
        self.stop_btn = ModernButton("⏹️ Stop", "#f44336")
        self.stop_btn.clicked.connect(self.stop_splitting)
        self.stop_btn.setEnabled(False)
        
        self.open_folder_btn = ModernButton("📂 Open Output Folder", "#9C27B0")
        self.open_folder_btn.clicked.connect(self.open_output_folder)
        self.open_folder_btn.setEnabled(False)

        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.stop_btn)
        button_layout.addWidget(self.open_folder_btn)
        button_layout.addStretch()

        # Add all to main layout
        layout.addWidget(file_group)
        layout.addWidget(settings_group)
        layout.addLayout(button_layout)

        widget.setLayout(layout)
        return widget

    def create_progress_section(self):
        """Create the progress and logging section"""
        widget = QWidget()
        layout = QVBoxLayout()

        # Progress group
        progress_group = QGroupBox("📊 Progress")
        progress_layout = QVBoxLayout()

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("font-weight: bold; color: #333;")

        progress_layout.addWidget(self.status_label)
        progress_layout.addWidget(self.progress_bar)
        progress_group.setLayout(progress_layout)

        # System info
        self.system_label = QLabel(self.get_system_info())
        self.system_label.setStyleSheet("font-size: 8pt; color: #666;")

        # Log group
        log_group = QGroupBox("📝 Processing Log")
        log_layout = QVBoxLayout()

        self.log_text = QTextEdit()
        self.log_text.setMaximumHeight(200)
        self.log_text.setPlaceholderText("Processing logs will appear here...")

        # Log controls
        log_controls = QHBoxLayout()
        clear_log_btn = QPushButton("Clear Log")
        clear_log_btn.clicked.connect(self.log_text.clear)
        save_log_btn = QPushButton("Save Log")
        save_log_btn.clicked.connect(self.save_log)
        
        log_controls.addWidget(clear_log_btn)
        log_controls.addWidget(save_log_btn)
        log_controls.addStretch()

        log_layout.addWidget(self.log_text)
        log_layout.addLayout(log_controls)
        log_group.setLayout(log_layout)

        layout.addWidget(progress_group)
        layout.addWidget(self.system_label)
        layout.addWidget(log_group)

        widget.setLayout(layout)
        return widget

    def get_system_info(self):
        """Get system information"""
        try:
            cpu_count = psutil.cpu_count()
            memory = psutil.virtual_memory()
            memory_gb = memory.total / (1024**3)
            return f"System: {cpu_count} CPU cores, {memory_gb:.1f}GB RAM"
        except:
            return "System information unavailable"

    def check_system_requirements(self):
        """Check system requirements on startup"""
        ffmpeg_ok, ffmpeg_msg, _ = FFmpegValidator.check_ffmpeg()
        
        if ffmpeg_ok:
            self.log_message(f"✅ {ffmpeg_msg}")
        else:
            self.log_message(f"❌ {ffmpeg_msg}")
            QMessageBox.warning(
                self, "FFmpeg Required",
                f"FFmpeg is required but not found:\n\n{ffmpeg_msg}\n\n"
                "Please install FFmpeg and ensure it's in your system PATH.\n"
                "Visit: https://ffmpeg.org/download.html"
            )

    def select_video_file(self):
        """Select input video file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Video File", "",
            "Video Files (*.mp4 *.avi *.mov *.mkv *.wmv *.flv *.webm *.m4v);;All Files (*)"
        )
        
        if file_path:
            self.input_file = file_path
            filename = os.path.basename(file_path)
            self.file_label.setText(filename)
            self.file_label.setStyleSheet("color: #333; font-weight: bold;")
            
            # Get video info
            self.video_info = FFmpegValidator.get_video_info(file_path)
            if self.video_info:
                duration_str = f"{self.video_info['duration']:.1f}s"
                size_mb = self.video_info['size'] / (1024 * 1024)
                info_text = (f"📹 {self.video_info['width']}×{self.video_info['height']}, "
                           f"{duration_str}, {size_mb:.1f}MB, "
                           f"{self.video_info['video_codec']}/{self.video_info['audio_codec']}")
                self.info_label.setText(info_text)
            
            self.log_message(f"📁 Selected: {filename}")

    def select_export_folder(self):
        """Select export folder"""
        folder_path = QFileDialog.getExistingDirectory(self, "Select Export Folder")
        
        if folder_path:
            self.output_dir = folder_path
            self.export_label.setText(folder_path)
            self.export_label.setStyleSheet("color: #333; font-weight: bold;")
            self.log_message(f"📂 Export folder: {folder_path}")

    def start_splitting(self):
        """Start the video splitting process"""
        # Validation
        if not self.input_file:
            QMessageBox.warning(self, "No File", "Please select a video file first!")
            return

        if not self.output_dir:
            QMessageBox.warning(self, "No Folder", "Please select an export folder first!")
            return

        if not os.path.exists(self.input_file):
            QMessageBox.warning(self, "File Missing", "Selected video file no longer exists!")
            return

        # Check FFmpeg
        ffmpeg_ok, ffmpeg_msg, _ = FFmpegValidator.check_ffmpeg()
        if not ffmpeg_ok:
            QMessageBox.critical(self, "FFmpeg Error", ffmpeg_msg)
            return

        # Get settings
        clip_duration = self.duration_input.value()
        num_clips = self.num_clips_input.value()
        overlap = self.overlap_input.value()
        use_copy = self.mode_combo.currentIndex() == 0
        quality = self.quality_combo.currentText().lower()

        # Validate overlap
        if overlap >= clip_duration:
            QMessageBox.warning(self, "Invalid Overlap", 
                              "Overlap must be less than clip duration!")
            return

        # Check disk space
        try:
            free_space = shutil.disk_usage(self.output_dir).free / (1024**3)  # GB
            if free_space < 1:
                reply = QMessageBox.question(
                    self, "Low Disk Space",
                    f"Warning: Only {free_space:.1f}GB free space.\n\nContinue anyway?",
                    QMessageBox.Yes | QMessageBox.No
                )
                if reply == QMessageBox.No:
                    return
        except:
            pass

        # Create timestamped output directory
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_subdir = os.path.join(self.output_dir, f"clips_{clip_duration}s_{timestamp}")

        # Reset UI
        self.progress_bar.setValue(0)
        self.log_text.clear()
        self.set_controls_enabled(False)

        # Start worker
        self.worker = SplitWorker(
            self.input_file, clip_duration, num_clips, output_subdir,
            overlap, use_copy, quality
        )
        
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.clip_completed.connect(self.on_clip_completed)
        self.worker.start()

        self.ui_timer.start(100)
        self.log_message("🚀 Starting video splitting process...")

    def stop_splitting(self):
        """Stop the splitting process"""
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.log_message("⏹️ Stop requested...")
            self.status_label.setText("Stopping...")

    def open_output_folder(self):
        """Open the output folder in file explorer"""
        if hasattr(self, 'last_output_dir') and os.path.exists(self.last_output_dir):
            if sys.platform == 'win32':
                os.startfile(self.last_output_dir)
            elif sys.platform == 'darwin':
                subprocess.run(['open', self.last_output_dir])
            else:
                subprocess.run(['xdg-open', self.last_output_dir])

    def on_finished(self, output_dir, clips_created):
        """Handle successful completion"""
        self.ui_timer.stop()
        self.set_controls_enabled(True)
        self.last_output_dir = output_dir
        self.open_folder_btn.setEnabled(True)
        
        self.log_message(f"✅ Successfully created {clips_created} clips!")
        self.log_message(f"📂 Output folder: {output_dir}")
        
        QMessageBox.information(
            self, "Success!",
            f"Video splitting completed!\n\n"
            f"Created {clips_created} clips in:\n{output_dir}"
        )

    def on_error(self, error_msg):
        """Handle errors"""
        self.ui_timer.stop()
        self.set_controls_enabled(True)
        
        self.log_message(f"❌ Error: {error_msg}")
        QMessageBox.critical(self, "Error", f"An error occurred:\n\n{error_msg}")

    def on_status_update(self, status):
        """Handle status updates"""
        self.status_label.setText(status)
        self.log_message(status)

    def on_clip_completed(self, clip_number, file_info):
        """Handle individual clip completion"""
        self.log_message(f"✅ Clip {clip_number} completed: {file_info}")

    def log_message(self, message):
        """Add message to log with timestamp"""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        self.log_text.append(formatted_message)
        
        # Auto-scroll to bottom
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def save_log(self):
        """Save log to file"""
        if self.log_text.toPlainText():
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Log", "video_splitter_log.txt", "Text Files (*.txt)"
            )
            if file_path:
                try:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(self.log_text.toPlainText())
                    QMessageBox.information(self, "Success", f"Log saved to:\n{file_path}")
                except Exception as e:
                    QMessageBox.warning(self, "Error", f"Failed to save log:\n{str(e)}")

    def set_controls_enabled(self, enabled):
        """Enable/disable controls during processing"""
        self.select_file_btn.setEnabled(enabled)
        self.select_folder_btn.setEnabled(enabled)
        self.start_btn.setEnabled(enabled)
        self.stop_btn.setEnabled(not enabled)
        self.duration_input.setEnabled(enabled)
        self.num_clips_input.setEnabled(enabled)
        self.overlap_input.setEnabled(enabled)
        self.mode_combo.setEnabled(enabled)
        self.quality_combo.setEnabled(enabled)

    def update_ui(self):
        """Keep UI responsive during processing"""
        QApplication.processEvents()

    def closeEvent(self, event):
        """Handle application close"""
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "Processing Active",
                "Video processing is still active.\n\nForce quit?",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.worker.stop()
                self.worker.wait(3000)  # Wait up to 3 seconds
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


def main():
    """Main application entry point"""
    app = QApplication(sys.argv)
    
    # Set application properties
    app.setApplicationName("MP4 Video Splitter Pro")
    app.setApplicationVersion("3.0")
    app.setOrganizationName("VideoTools")
    
    # Apply dark theme on request or for modern look
    app.setStyle('Fusion')
    
    window = VideoSplitterApp()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()