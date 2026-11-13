import asyncio
import os
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# Load .env from same directory as this script
load_dotenv(Path(__file__).parent / ".env")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not set. Run configure.py to configure your API key.")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
client = genai.Client(api_key=GEMINI_API_KEY)
mcp = MCPServer("gemini-research-mcp", version="1.1.0")

SYSTEM_PROMPT = (
    "You are a research assistant. Summarize the topic accurately and evenhandedly. "
    "Be factual, do not editorialize. Include key people, dates, and events. "
    "Do not pad the response — match length to complexity."
)

Detail = Literal["low", "normal", "high"]
WORDS = {"low": "~300", "normal": "~800", "high": "~2000"}


async def generate(prompt: str, tool: genai_types.Tool) -> genai_types.GenerateContentResponse:
    try:
        return await client.aio.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, tools=[tool]),
        )
    except genai_errors.APIError as e:
        raise ToolError(f"Gemini API error {e.code}: {e.message}") from e


def response_text(response: genai_types.GenerateContentResponse) -> str:
    if not response.text:
        reason = response.candidates[0].finish_reason if response.candidates else "no candidates"
        raise ToolError(f"Gemini returned no text (finish reason: {reason}).")
    return response.text


async def resolve_redirect(http: httpx.AsyncClient, uri: str) -> str:
    """Resolve a grounding redirect link to the target URL; keep the link if that fails."""
    try:
        response = await http.head(uri)
    except httpx.HTTPError:
        return uri
    return response.headers.get("location", uri) if response.is_redirect else uri


async def sources(response: genai_types.GenerateContentResponse) -> str:
    """Format the grounding sources as a Markdown list, deduplicated by target URL."""
    candidate = response.candidates[0] if response.candidates else None
    metadata = candidate.grounding_metadata if candidate else None
    webs = [c.web for c in (metadata.grounding_chunks if metadata else None) or [] if c.web and c.web.uri]
    if not webs:
        return ""
    async with httpx.AsyncClient(timeout=5) as http:
        urls = await asyncio.gather(*(resolve_redirect(http, web.uri) for web in webs))
    seen = {}
    for web, url in zip(webs, urls):
        seen.setdefault(url, web.title or url)
    return "\n\n**Sources:**\n" + "\n".join(f"- [{title}]({url})" for url, title in seen.items())


@mcp.tool(structured_output=False)
async def research(query: str, detail: Detail = "low") -> str:
    """Search the web and return a factual summary of a topic, with sources.

    Use for anything past Claude's knowledge cutoff or current events.
    detail controls summary length: low=~300 words, normal=~800, high=~2000.
    """
    response = await generate(
        f"Research the following topic using Google Search and write a summary of "
        f"approximately {WORDS[detail]} words:\n\n{query}",
        genai_types.Tool(google_search=genai_types.GoogleSearch()),
    )
    return response_text(response) + await sources(response)


@mcp.tool(structured_output=False)
async def research_url(url: str, detail: Detail = "low") -> str:
    """Fetch a specific URL and summarize its content.

    detail controls summary length: low=~300 words, normal=~800, high=~2000.
    """
    response = await generate(
        f"Summarize the content of the following URL in approximately {WORDS[detail]} words:\n\n{url}",
        genai_types.Tool(url_context=genai_types.UrlContext()),
    )
    # Without a successful retrieval Gemini would summarize from guesswork
    candidate = response.candidates[0] if response.candidates else None
    url_metadata = candidate.url_context_metadata if candidate else None
    statuses = [m.url_retrieval_status for m in (url_metadata.url_metadata if url_metadata else None) or []]
    if genai_types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_SUCCESS not in statuses:
        raise ToolError(f"Could not retrieve {url} (status: {statuses or 'unknown'}).")
    return response_text(response)


if __name__ == "__main__":
    mcp.run()
