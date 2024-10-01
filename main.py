# Filename: ai_assistant_gui.py

import sys
import os
import asyncio
import threading
import datetime
import json
import shutil
import logging
import re
import time
import base64
import difflib
import subprocess
import glob
import mimetypes
from typing import Tuple, Dict, Any, List, Optional

# PyQt5 imports
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QTextEdit, QLineEdit, QPushButton, QAction, QFileDialog,
                             QLabel, QMenuBar, QMenu, QMessageBox, QScrollArea, QCheckBox)
from PyQt5.QtCore import Qt, QEventLoop
from PyQt5.QtGui import QIcon, QTextCursor

# qasync for integrating asyncio with PyQt5
from qasync import QEventLoop, asyncSlot

# Other necessary imports
from dotenv import load_dotenv
from anthropic import Anthropic, APIStatusError, APIError
from tavily import TavilyClient
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

# Additional imports for TTS and speech recognition
import speech_recognition as sr
import websockets
from PIL import Image
import io

# Configure logging
logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables from .env file
load_dotenv()

# Initialize the Anthropic client
anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
if not anthropic_api_key:
    raise ValueError("ANTHROPIC_API_KEY not found in environment variables")
anthropic_client = Anthropic(api_key=anthropic_api_key)

# Initialize the Tavily client
tavily_api_key = os.getenv("TAVILY_API_KEY")
if not tavily_api_key:
    raise ValueError("TAVILY_API_KEY not found in environment variables")
tavily_client = TavilyClient(api_key=tavily_api_key)

# 11 Labs TTS
ELEVEN_LABS_API_KEY = os.getenv('ELEVEN_LABS_API_KEY')
VOICE_ID = 'YOUR_VOICE_ID'  # Replace with your voice ID
MODEL_ID = 'eleven_turbo_v2_5'

# Global variables
console = Console()
MAX_CONTEXT_TOKENS = 200000  # Adjust as needed

# Token tracking variables
main_model_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
tool_checker_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
code_editor_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
code_execution_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}

# Conversation and file management
conversation_history = []
file_contents = {}
code_editor_memory = []
code_editor_files = set()
running_processes = {}

# Models
MAINMODEL = "claude-3-5-sonnet-20240620"
TOOLCHECKERMODEL = "claude-3-5-sonnet-20240620"
CODEEDITORMODEL = "claude-3-5-sonnet-20240620"
CODEEXECUTIONMODEL = "claude-3-5-sonnet-20240620"

# System prompts (Truncated for brevity)
BASE_SYSTEM_PROMPT = "You are Claude, an AI assistant..."

# Tools definition (Truncated for brevity)
tools = [
    {
        "name": "create_folders",
        "description": "Create new folders...",
        # Input schema
    },
    # Add other tools here
]

# Helper functions
def setup_virtual_environment() -> Tuple[str, str]:
    venv_name = "code_execution_env"
    venv_path = os.path.join(os.getcwd(), venv_name)
    try:
        if not os.path.exists(venv_path):
            subprocess.run([sys.executable, '-m', 'venv', venv_name], check=True)
        activate_script = os.path.join(venv_path, "Scripts", "activate") if os.name == 'nt' else os.path.join(venv_path, "bin", "activate")
        return venv_path, activate_script
    except Exception as e:
        logging.error(f"Error setting up virtual environment: {str(e)}")
        raise

def encode_image_to_base64(image_path):
    try:
        with Image.open(image_path) as img:
            max_size = (1024, 1024)
            img.thumbnail(max_size, Image.ANTIALIAS)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='JPEG')
            return base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
    except Exception as e:
        return f"Error encoding image: {str(e)}"

# Define other helper functions as needed

# MainWindow class
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Claude-3.5-Sonnet Engineer Chat")
        self.resize(1000, 800)
        self.setWindowIcon(QIcon('icon.png'))  # Set an appropriate icon if available

        # Initialize UI components
        self.init_ui()

        # State variables
        self.voice_mode = False
        self.tts_enabled = False
        self.automode = False
        self.max_iterations = 25

        # Initialize speech recognition
        self.recognizer = None
        self.microphone = None

    def init_ui(self):
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Main layout
        main_layout = QVBoxLayout()
        central_widget.setLayout(main_layout)

        # Chat display area
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        main_layout.addWidget(self.chat_display)

        # User input area
        input_layout = QHBoxLayout()
        self.user_input = QLineEdit()
        self.send_button = QPushButton("Send")
        self.voice_button = QPushButton("Voice Input")
        input_layout.addWidget(self.user_input)
        input_layout.addWidget(self.send_button)
        input_layout.addWidget(self.voice_button)
        main_layout.addLayout(input_layout)

        # Control panel
        control_layout = QHBoxLayout()
        self.tts_checkbox = QCheckBox("Enable TTS")
        self.reset_button = QPushButton("Reset Conversation")
        self.save_button = QPushButton("Save Chat")
        control_layout.addWidget(self.tts_checkbox)
        control_layout.addWidget(self.reset_button)
        control_layout.addWidget(self.save_button)
        main_layout.addLayout(control_layout)

        # Connect signals
        self.send_button.clicked.connect(self.on_send_clicked)
        self.voice_button.clicked.connect(self.on_voice_clicked)
        self.tts_checkbox.stateChanged.connect(self.on_tts_toggled)
        self.reset_button.clicked.connect(self.on_reset_clicked)
        self.save_button.clicked.connect(self.on_save_clicked)

        # Menu bar
        self.create_menu()

    def create_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu('File')
        automode_action = QAction('Enter Automode', self)
        automode_action.triggered.connect(self.on_automode_triggered)
        file_menu.addAction(automode_action)

    @asyncSlot()
    async def on_send_clicked(self):
        user_text = self.user_input.text()
        if user_text.strip() == '':
            return
        self.user_input.clear()
        self.append_message("User", user_text)
        response, _ = await self.chat_with_claude(user_text)
        self.append_message("Claude", response)

    @asyncSlot()
    async def on_voice_clicked(self):
        if not self.voice_mode:
            self.voice_mode = True
            self.voice_button.setText("Stop Voice Input")
            await self.voice_input_loop()
        else:
            self.voice_mode = False
            self.voice_button.setText("Voice Input")

    def on_tts_toggled(self, state):
        self.tts_enabled = bool(state)

    def on_reset_clicked(self):
        self.reset_conversation()
        QMessageBox.information(self, "Conversation Reset", "Conversation has been reset.")

    def on_save_clicked(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Save Chat Log", "", "Markdown Files (*.md)")
        if filename:
            self.save_chat(filename)
            QMessageBox.information(self, "Chat Saved", f"Chat has been saved to {filename}")

    @asyncSlot()
    async def on_automode_triggered(self):
        iterations, ok = self.get_number_dialog("Enter Automode", "Number of iterations:")
        if ok:
            self.max_iterations = iterations
            self.automode = True
            user_input, ok = self.get_text_dialog("Automode", "Enter the goal for automode:")
            if ok:
                await self.run_automode(user_input)
            else:
                self.automode = False

    def get_number_dialog(self, title, label):
        num, ok = QInputDialog.getInt(self, title, label, min=1, max=100)
        return num, ok

    def get_text_dialog(self, title, label):
        text, ok = QInputDialog.getText(self, title, label)
        return text, ok

    def append_message(self, sender, message):
        self.chat_display.moveCursor(QTextCursor.End)
        self.chat_display.insertHtml(f"<b>{sender}:</b> {message}<br><br>")
        self.chat_display.moveCursor(QTextCursor.End)

    @asyncSlot()
    async def voice_input_loop(self):
        self.initialize_speech_recognition()
        while self.voice_mode:
            user_input = await self.voice_input()
            if user_input:
                self.append_message("User (Voice)", user_input)
                response, _ = await self.chat_with_claude(user_input)
                self.append_message("Claude", response)
            else:
                self.voice_mode = False
                self.voice_button.setText("Voice Input")
                break

    def initialize_speech_recognition(self):
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=1)
        logging.info("Speech recognition initialized")

    async def voice_input(self):
        try:
            with self.microphone as source:
                self.append_message("System", "Listening... Speak now.")
                audio = self.recognizer.listen(source, timeout=5)
            self.append_message("System", "Processing speech...")
            text = self.recognizer.recognize_google(audio)
            return text.lower()
        except Exception as e:
            self.append_message("Error", f"Voice input error: {str(e)}")
            return None

    async def chat_with_claude(self, user_input, image_path=None):
        global conversation_history, main_model_tokens

        current_conversation = []
        if image_path:
            image_base64 = encode_image_to_base64(image_path)
            if image_base64.startswith("Error"):
                self.append_message("Error", image_base64)
                return "I'm sorry, there was an error processing the image. Please try again.", False

            image_message = {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_base64
                        }
                    },
                    {
                        "type": "text",
                        "text": f"User input for image: {user_input}"
                    }
                ]
            }
            current_conversation.append(image_message)
        else:
            current_conversation.append({"role": "user", "content": user_input})

        messages = conversation_history + current_conversation

        try:
            response = anthropic_client.beta.prompt_caching.messages.create(
                model=MAINMODEL,
                max_tokens=8000,
                system=[
                    {
                        "type": "text",
                        "text": BASE_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"}
                    },
                    {
                        "type": "text",
                        "text": json.dumps(tools),
                        "cache_control": {"type": "ephemeral"}
                    }
                ],
                messages=messages,
                tools=tools,
                tool_choice={"type": "auto"},
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"}
            )
            # Update token usage for MAINMODEL
            main_model_tokens['input'] += response.usage.input_tokens
            main_model_tokens['output'] += response.usage.output_tokens
            main_model_tokens['cache_write'] = response.usage.cache_creation_input_tokens
            main_model_tokens['cache_read'] = response.usage.cache_read_input_tokens
        except Exception as e:
            self.append_message("Error", f"API Error: {str(e)}")
            return "I'm sorry, there was an error communicating with the AI. Please try again.", False

        assistant_response = ""
        for content_block in response.content:
            if content_block.type == "text":
                assistant_response += content_block.text

        # Handle TTS if enabled
        if self.tts_enabled:
            await self.text_to_speech(assistant_response)

        # Update conversation history
        conversation_history.extend(current_conversation)
        conversation_history.append({"role": "assistant", "content": assistant_response})

        return assistant_response, False

    async def text_to_speech(self, text):
        # Implement text-to-speech using ElevenLabs API
        pass  # For brevity, implementation is omitted

    def reset_conversation(self):
        global conversation_history, main_model_tokens
        conversation_history = []
        main_model_tokens = {'input': 0, 'output': 0}
        # Reset other state variables as needed
        self.chat_display.clear()

    def save_chat(self, filename):
        # Save conversation history to a Markdown file
        formatted_chat = "# Claude-3.5-Sonnet Engineer Chat Log\n\n"
        for message in conversation_history:
            if message['role'] == 'user':
                formatted_chat += f"## User\n\n{message['content']}\n\n"
            elif message['role'] == 'assistant':
                formatted_chat += f"## Claude\n\n{message['content']}\n\n"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(formatted_chat)

    @asyncSlot()
    async def run_automode(self, user_input):
        self.append_message("Automode", f"Starting automode with {self.max_iterations} iterations.")
        iteration_count = 0
        while self.automode and iteration_count < self.max_iterations:
            response, exit_continuation = await self.chat_with_claude(user_input)
            self.append_message("Claude", response)
            if "AUTOMODE_COMPLETE" in response:
                self.append_message("Automode", "Automode completed.")
                self.automode = False
                break
            else:
                user_input = "Continue with the next step."
            iteration_count += 1
        if iteration_count >= self.max_iterations:
            self.append_message("Automode", "Max iterations reached. Exiting automode.")
            self.automode = False

    # Implement other methods as needed

def main():
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = MainWindow()
    window.show()
    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
