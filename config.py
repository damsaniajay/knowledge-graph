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

# Optional PostgreSQL audit/history tables (see sql/001_tracking.sql)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TRACKING_ENABLED = os.getenv("TRACKING_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Optional Graphiti episodes for semantic delta hints (does not replace Neo4j graph)
USE_GRAPHITI_DELTA = os.getenv("USE_GRAPHITI_DELTA", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")

# Flow derivation: LLM when key present unless explicitly disabled
_use_llm_env = os.getenv("USE_LLM_FLOWS", "").strip().lower()
USE_LLM_FLOWS = (
    _use_llm_env in ("1", "true", "yes")
    if _use_llm_env
    else bool(OPENAI_API_KEY)
)

# If true, story upload without flows[] creates a proposal instead of writing flows immediately
FLOW_REQUIRE_APPROVAL = os.getenv("FLOW_REQUIRE_APPROVAL", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)


def use_llm_entity_match() -> bool:
    """Match uploads to existing entities (versioning) via LLM when key is set."""
    _env = os.getenv("USE_LLM_ENTITY_MATCH", "").strip().lower()
    if _env in ("0", "false", "no"):
        return False
    if _env in ("1", "true", "yes"):
        return True
    return USE_LLM_FLOWS and bool(OPENAI_API_KEY)
