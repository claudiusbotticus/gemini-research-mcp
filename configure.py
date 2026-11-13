from getpass import getpass
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"


def main():
    print("Get a free API key at: https://aistudio.google.com/apikey")
    key = getpass("Paste your Gemini API key (input hidden): ").strip()
    if not key:
        raise SystemExit("No API key entered, nothing saved.")

    # Keep other settings such as GEMINI_MODEL, replace only the key
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    lines = [line for line in lines if not line.startswith("GEMINI_API_KEY=")]
    ENV_PATH.write_text("\n".join([f"GEMINI_API_KEY={key}", *lines]) + "\n")
    ENV_PATH.chmod(0o600)
    print(f"Saved to {ENV_PATH}. Restart your MCP client to pick it up.")


if __name__ == "__main__":
    main()
