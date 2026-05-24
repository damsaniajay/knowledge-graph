import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Add the existing IntelliQ backend to the Python path so we can import its services
BACKEND_PATH = r"C:\Intelli-Q-Airtel-POC\gui\backend"
if BACKEND_PATH not in sys.path:
    sys.path.append(BACKEND_PATH)

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")
