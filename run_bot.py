from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)

from app.setup_wizard import ensure_env_configured
from app.engine import run


if __name__ == "__main__":
    ensure_env_configured()
    run()
