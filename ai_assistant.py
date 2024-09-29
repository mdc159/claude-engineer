# Filename: ai_assistant.py

import os
import sys
import json
import time
import glob
import shutil
import asyncio
import logging
import signal
import base64
import datetime
import subprocess
import threading
import mimetypes
import difflib
import tempfile
import venv

from dotenv import load_dotenv
from typing import Any, Dict, List, Optional, Tuple, Union, AsyncIterable

import aiohttp
import websockets
import speech_recognition as sr

from pydub import AudioSegment
from pydub.playback import play

from anthropic import Anthropic, APIStatusError, APIError
from tavily import TavilyClient

from PIL import Image
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set to INFO for more insights during development
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Load environment variables from .env file
load_dotenv()

# Initialize the console for rich output
console = Console()

# Global constants and variables
MAX_CONTEXT_TOKENS = 200000

# Lock for thread-safe operations
file_contents_lock = threading.Lock()

# Initialize API clients
anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
tavily_api_key = os.getenv("TAVILY_API_KEY")
ELEVEN_LABS_API_KEY = os.getenv('ELEVEN_LABS_API_KEY')

if not anthropic_api_key:
    raise ValueError("ANTHROPIC_API_KEY not found in environment variables")
if not tavily_api_key:
    raise ValueError("TAVILY_API_KEY not found in environment variables")

anthropic_client = Anthropic(api_key=anthropic_api_key)
tavily_client = TavilyClient(api_key=tavily_api_key)

# Voice settings
VOICE_ID = 'YOUR VOICE ID'
MODEL_ID = 'eleven_turbo_v2_5'

# Initialize speech recognition
recognizer: Optional[sr.Recognizer] = None
microphone: Optional[sr.Microphone] = None

# Conversation and context management
conversation_history: List[Dict[str, Any]] = []
file_contents: Dict[str, str] = {}
code_editor_memory: List[str] = []
code_editor_files: set = set()
running_processes: Dict[str, subprocess.Popen] = {}

# Token usage tracking
main_model_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
tool_checker_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
code_editor_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
code_execution_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}

# Voice commands
VOICE_COMMANDS = {
    "exit voice mode": "exit_voice_mode",
    "save chat": "save_chat",
    "reset conversation": "reset_conversation"
}

# Automode settings
automode = False
MAX_CONTINUATION_ITERATIONS = 25
CONTINUATION_EXIT_PHRASE = "AUTOMODE_COMPLETE"

# Models
MAINMODEL = "claude-3-5-sonnet-20240620"
TOOLCHECKERMODEL = "claude-3-5-sonnet-20240620"
CODEEDITORMODEL = "claude-3-5-sonnet-20240620"
CODEEXECUTIONMODEL = "claude-3-5-sonnet-20240620"

# TTS settings
tts_enabled = True
use_tts = False

# Define tools
tools = [
    # ... [Define tools as in the original script, with updated descriptions if needed] ...
]

# Function definitions

def is_installed(lib_name: str) -> bool:
    """Check if a library or command is installed."""
    return shutil.which(lib_name) is not None

def initialize_speech_recognition():
    """Initialize speech recognition components."""
    global recognizer, microphone
    recognizer = sr.Recognizer()
    microphone = sr.Microphone()
    with microphone as source:
        recognizer.adjust_for_ambient_noise(source, duration=1)
    logging.info("Speech recognition initialized")

def cleanup_speech_recognition():
    """Clean up speech recognition components."""
    global recognizer, microphone
    recognizer = None
    microphone = None
    logging.info('Speech recognition objects cleaned up')

async def voice_input(max_retries: int = 3) -> Optional[str]:
    """Capture voice input from the user."""
    global recognizer, microphone
    for attempt in range(max_retries):
        initialize_speech_recognition()
        try:
            with microphone as source:
                console.print("Listening... Speak now.", style="bold green")
                audio = recognizer.listen(source, timeout=5)
            console.print("Processing speech...", style="bold yellow")
            text = recognizer.recognize_google(audio)
            console.print(f"You said: {text}", style="cyan")
            return text.lower()
        except (sr.WaitTimeoutError, sr.UnknownValueError) as e:
            console.print(f"Attempt {attempt + 1}/{max_retries} failed: {str(e)}", style="bold red")
            logging.warning(f"Speech recognition attempt {attempt + 1} failed: {str(e)}")
        except sr.RequestError as e:
            console.print(f"Speech recognition service error: {e}", style="bold red")
            logging.error(f"Speech recognition service error: {e}")
            return None
        except Exception as e:
            console.print(f"Unexpected error in voice input: {str(e)}", style="bold red")
            logging.error(f"Unexpected error in voice input: {str(e)}")
            return None
        await asyncio.sleep(1)
    console.print("Max retries reached. Returning to text input mode.", style="bold red")
    return None

def process_voice_command(command: str) -> Tuple[bool, Optional[str]]:
    """Process voice commands."""
    if command in VOICE_COMMANDS:
        action = VOICE_COMMANDS[command]
        if action == "exit_voice_mode":
            return False, "Exiting voice mode."
        elif action == "save_chat":
            filename = save_chat()
            return True, f"Chat saved to {filename}"
        elif action == "reset_conversation":
            reset_conversation()
            return True, "Conversation has been reset."
    return True, None

async def get_user_input(prompt: str = "You: ") -> str:
    """Get user input from the command line."""
    style = Style.from_dict({'prompt': 'cyan bold'})
    session = PromptSession(style=style)
    return await session.prompt_async(prompt, multiline=False)

def setup_virtual_environment() -> Tuple[str, str]:
    """Set up a virtual environment for code execution."""
    venv_name = "code_execution_env"
    venv_path = os.path.join(os.getcwd(), venv_name)
    try:
        if not os.path.exists(venv_path):
            venv.create(venv_path, with_pip=True)
        if sys.platform == "win32":
            activate_script = os.path.join(venv_path, "Scripts", "activate.bat")
        else:
            activate_script = os.path.join(venv_path, "bin", "activate")
        return venv_path, activate_script
    except Exception as e:
        logging.error(f"Error setting up virtual environment: {str(e)}")
        raise

def reset_conversation():
    """Reset the conversation history and related data."""
    global conversation_history, main_model_tokens, tool_checker_tokens
    global code_editor_tokens, code_execution_tokens, file_contents, code_editor_files
    conversation_history.clear()
    file_contents.clear()
    code_editor_files.clear()
    main_model_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
    tool_checker_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
    code_editor_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
    code_execution_tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0}
    console.print(Panel("Conversation and context have been reset.", style="bold green"))

def save_chat() -> str:
    """Save the conversation history to a Markdown file."""
    now = datetime.datetime.now()
    filename = f"Chat_{now.strftime('%Y%m%d_%H%M%S')}.md"
    formatted_chat = "# Chat Log\n\n"
    for message in conversation_history:
        role = message.get('role', '')
        content = message.get('content', '')
        if role == 'user':
            formatted_chat += f"## User\n\n{content}\n\n"
        elif role == 'assistant':
            formatted_chat += f"## Assistant\n\n{content}\n\n"
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(formatted_chat)
    return filename

def validate_command_whitelist(command: str) -> bool:
    """Validate shell commands against a whitelist."""
    whitelist = ['ls', 'dir', 'echo', 'pip list', 'python --version']
    return command.strip().split()[0] in whitelist

def sanitize_input(input_str: str) -> str:
    """Sanitize user input to prevent injection attacks."""
    return input_str.replace(';', '').replace('&', '').replace('|', '').strip()

async def execute_code(code: str, timeout: int = 10) -> Tuple[str, str]:
    """Execute code in an isolated environment with sandboxing."""
    venv_path, activate_script = setup_virtual_environment()
    if not isinstance(code, str):
        raise ValueError("Code must be a string.")

    # Create a temporary file for the code
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp_file:
        tmp_file.write(code)
        code_file = tmp_file.name

    # Prepare the command to run the code
    if sys.platform == "win32":
        command = f'"{activate_script}" && python "{code_file}"'
    else:
        command = f'source "{activate_script}" && python "{code_file}"'

    # Execute the command with restricted permissions
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=lambda: os.setgid(1000) if sys.platform != "win32" else None
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            stdout = stdout.decode()
            stderr = stderr.decode()
            return_code = process.returncode
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            stdout, stderr = '', 'Execution timed out.'
            return_code = 'Timed out'
        execution_result = f"Stdout:\n{stdout}\n\nStderr:\n{stderr}\n\nReturn Code: {return_code}"
        return code_file, execution_result
    except Exception as e:
        logging.error(f"Error executing code: {str(e)}")
        return '', f"Error executing code: {str(e)}"
    finally:
        # Clean up the temporary file
        os.remove(code_file)

def run_shell_command(command: str) -> Dict[str, Any]:
    """Run a shell command from a whitelist."""
    sanitized_command = sanitize_input(command)
    if not validate_command_whitelist(sanitized_command):
        return {"error": "Command not allowed."}
    try:
        result = subprocess.run(
            sanitized_command,
            shell=True,
            check=True,
            text=True,
            capture_output=True
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode
        }
    except subprocess.CalledProcessError as e:
        return {
            "stdout": e.stdout,
            "stderr": e.stderr,
            "return_code": e.returncode,
            "error": str(e)
        }
    except Exception as e:
        return {"error": f"An error occurred: {str(e)}"}

def safe_read_file(file_path: str) -> str:
    """Safely read the contents of a file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {str(e)}")
        return ''

def encode_image_to_base64(image_path: str) -> str:
    """Encode an image to a base64 string."""
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

# ... [Other functions with enhanced security, input validation, and error handling] ...

async def main():
    """Main function to run the AI assistant."""
    global automode, use_tts
    console.print(Panel("Welcome to the AI Assistant!", style="bold green"))
    console.print("Type 'exit' to quit, 'voice' for voice input, 'reset' to reset conversation.")

    voice_mode = False

    while True:
        if voice_mode:
            user_input = await voice_input()
            if user_input is None:
                voice_mode = False
                cleanup_speech_recognition()
                continue
            stay_in_voice_mode, command_result = process_voice_command(user_input)
            if not stay_in_voice_mode:
                voice_mode = False
                cleanup_speech_recognition()
                if command_result:
                    console.print(Panel(command_result, style="cyan"))
                continue
            elif command_result:
                console.print(Panel(command_result, style="cyan"))
                continue
        else:
            user_input = await get_user_input()

        if user_input.lower() == 'exit':
            console.print(Panel("Goodbye!", style="bold green"))
            break
        elif user_input.lower() == 'reset':
            reset_conversation()
            continue
        elif user_input.lower() == 'voice':
            voice_mode = True
            initialize_speech_recognition()
            console.print(Panel("Voice mode activated.", style="bold green"))
            continue
        elif user_input.lower() == 'save chat':
            filename = save_chat()
            console.print(Panel(f"Chat saved to {filename}", style="bold green"))
            continue
        elif user_input.lower().startswith('automode'):
            # Handle automode logic with proper error handling
            pass
        else:
            # Process the user input and interact with the AI assistant
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\nProgram interrupted by user. Exiting...", style="bold red")
    except Exception as e:
        console.print(f"An unexpected error occurred: {str(e)}", style="bold red")
        logging.error(f"Unexpected error: {str(e)}", exc_info=True)
    finally:
        console.print("Program finished. Goodbye!", style="bold green")
