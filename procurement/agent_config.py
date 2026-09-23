"""Read a server-side key without logging or returning it to the browser."""
import os
import constants


def api_key():
    key = os.environ.get('OPENAI_API_KEY', '').strip() or constants.OPENAI_API_KEY.strip()
    if key:
        return key
    path = constants.BASE_DIR / '.openai-key'
    return path.read_text(encoding='utf-8').strip() if path.is_file() else ''


MODEL = 'gpt-5-mini'
BUDGET_MICRO = 5_000_000  # $5, agent feature only; not the entire OpenAI project.
RESERVATION_MICRO = 100_000  # $0.10 per turn, settled using API usage on success.
MAX_ROUNDS = 4
MAX_OUTPUT = 2000
MAX_BODY_BYTES = 64000
