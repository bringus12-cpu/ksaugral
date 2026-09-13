import sys

from app.setup_wizard import ensure_env_configured


if __name__ == "__main__":
    ensure_env_configured(force="--force" in sys.argv)
