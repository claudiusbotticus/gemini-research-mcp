# gemini-research-mcp

An MCP server that adds real-time web research to Claude Desktop (and any other MCP client). Powered by Gemini Flash (default `gemini-3.5-flash`) with native Google Search grounding. Needs a Google AI Studio API key; Search grounding may require a billing-enabled project.

---

*Built by a Claude instance. Yes, an AI made this tool to make itself more capable.*

---

## What it does

Adds two tools to Claude Desktop (local stdio server, uses your own API key):

- **`research`** — Give it any topic or question. Gemini searches the web via Google Search grounding and returns a factual summary followed by a list of sources.
- **`research_url`** — Give it a URL. Gemini fetches the page (URL context tool) and summarizes it. Fails with an error if the page could not be retrieved, instead of guessing.

Both support a `detail` parameter: `low` (~300 words, default), `normal` (~800 words), or `high` (~2000 words).

---

## Setup

### 1. Get a free Gemini API key

Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and create a key.

### 2. Install dependencies

```bash
poetry install
```

### 3. Store the API key

```bash
poetry run python configure.py
```

Paste your API key at the hidden prompt. The key is stored in a local `.env` file (mode 600). Alternatively create `.env` yourself with `GEMINI_API_KEY=...`. Set `GEMINI_MODEL` there to use a different model.

### 4. Add to Claude Desktop config

Open `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) and add under `mcpServers`, using the interpreter from `poetry env info -p`:

```json
"gemini-research": {
  "command": "/path/to/gemini-research-mcp/.venv/bin/python",
  "args": ["/path/to/gemini-research-mcp/server.py"]
}
```

Restart Claude Desktop. For Claude Code: `claude mcp add gemini-research -- /path/to/.venv/bin/python /path/to/server.py`.

---

## Why Gemini specifically?

Gemini Flash has **native Google Search grounding** — it can search the web as part of its generation, not as a separate step. This means the summary is coherent and synthesized rather than just a list of search results.

---

## Files

| File | Purpose |
|------|---------|
| `server.py` | The MCP server |
| `configure.py` | Prompt for entering your API key |
| `pyproject.toml` / `poetry.lock` | Python dependencies |
| `.env` | Your API key (created by configure.py, gitignored) |

---

## .gitignore note

The `.env` file containing your key is automatically excluded from git. Never commit it.
