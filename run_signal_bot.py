from app.setup_wizard import ensure_env_configured
from app.telegram_signal_bot import run


if __name__ == "__main__":
    ensure_env_configured()
    run()
