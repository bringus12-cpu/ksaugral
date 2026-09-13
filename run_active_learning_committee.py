from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent / ".env.vantage.committee", override=True)

from app.active_learning_committee import run


if __name__ == "__main__":
    run()
