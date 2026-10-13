"""Web tools for the assistant: search -> fetch -> cache known-good sources.

All fetched/searched content is wrapped in <untrusted_web_content> so the
model treats it as data, never as instructions.
"""

from __future__ import annotations

import sqlite3
from html import escape
from urllib.parse import urlsplit

import httpx
from anthropic import beta_tool

from fetch import BlockedURL, Fetcher
from search import QuotaExceeded, SearchProvider
from source_judge import Verdict
from sources import SourceStore


def _untrusted(origin: str, body: str) -> str:
    return (f'<untrusted_web_content origin="{escape(origin)}">\n{body}\n'
            f"</untrusted_web_content>")


def make_web_tools(db: sqlite3.Connection, search: SearchProvider | None, judge, notify=print):
    """`judge(task, url_template, example_url, notes, content) -> Verdict` vets new sources.
    `notify(text)` reports things the user should see, like source check verdicts."""
    fetcher = Fetcher(db)
    sources = SourceStore(db)

    @beta_tool(eager_input_streaming=True)
    def find_sources(task: str) -> str:
        """Look up cached, previously verified URLs for a kind of task. Call this
        BEFORE web_search - it's free and faster.

        Args:
            task: Short description of the task, e.g. "weather forecast" or "currency exchange rate".
        """
        hits = sources.find(task)
        return "\n".join(s.render() for s in hits) if hits else "No cached sources for this task."

    @beta_tool(eager_input_streaming=True)
    def web_search(query: str, count: int = 5) -> str:
        """Search the web. Uses paid quota, so prefer find_sources first.

        Args:
            query: The search query.
            count: Number of results (1-10).
        """
        try:
            results = search.search(query, max(1, min(count, 10)))
        except QuotaExceeded as e:
            return f"Search unavailable: {e}"
        except httpx.HTTPError as e:
            return f"Search failed: {e}"
        if not results:
            return "No results."
        body = "\n\n".join(f"{i}. {r.title}\n   {r.url}\n   {r.snippet}"
                           for i, r in enumerate(results, 1))
        return _untrusted(f"search:{query}", body)

    @beta_tool(eager_input_streaming=True)
    def http_get(url: str, source_id: int | None = None, max_age_minutes: int = 30) -> str:
        """Fetch a public web page or JSON API and return its text. Results are cached
        locally; a cached copy younger than max_age_minutes is returned instead.

        Args:
            url: Full http(s) URL. If using a cached source, fill in its template placeholders.
            source_id: The id of the cached source this URL came from, if any (updates its reliability stats).
            max_age_minutes: Accept a cached copy up to this old. Use small values for live data.
        """
        try:
            text = fetcher.get(url, max_age_s=max_age_minutes * 60)
        except (BlockedURL, RuntimeError) as e:
            if source_id is not None:
                sources.record(source_id, ok=False)
            return f"Fetch failed: {e}"
        if source_id is not None:
            sources.record(source_id, ok=True)
        return _untrusted(url, text)

    @beta_tool(eager_input_streaming=True)
    def save_source(task: str, url_template: str, example_url: str, notes: str,
                    replaces_source_id: int | None = None) -> str:
        """Save a URL that worked for a task so it can be reused without searching.
        Only call after http_get on example_url returned useful data. Prefer reusable
        API endpoints over one-off pages. An independent check reviews the source and
        may reject it; tell the user the outcome.

        Args:
            task: Short description of the task, e.g. "weather forecast".
            url_template: The URL with {placeholders} for the parts that vary, e.g. ...?latitude={lat}&longitude={lon}.
            example_url: The concrete URL you successfully fetched.
            notes: How to fill in the placeholders and what the response contains.
            replaces_source_id: When revising a source (e.g. after user feedback), the id of the old source to replace.
        """
        cached = fetcher.cached(example_url, max_age_s=24 * 3600)
        if cached is None:
            return "Rejected: example_url has not been fetched successfully. Fetch it first."
        tmpl, ex = urlsplit(url_template), urlsplit(example_url)
        if tmpl.scheme != "https":
            return "Rejected: url_template must use https."
        if tmpl.hostname != ex.hostname:
            return "Rejected: url_template and example_url must be on the same host."

        verdict: Verdict = judge(task, url_template, example_url, notes, cached[0])
        status = "approved" if verdict.approved else "rejected"
        notify(f"Source check {status}: {verdict.reason}")
        if not verdict.approved:
            return f"Source check rejected it: {verdict.reason}"

        source_id = sources.save(task, url_template, notes, check=verdict.reason)
        if replaces_source_id is not None and replaces_source_id != source_id:
            sources.delete(replaces_source_id)
            return (f"Saved as source {source_id}, replacing source {replaces_source_id}. "
                    f"Check: {verdict.reason}")
        return f"Saved as source {source_id}. Check: {verdict.reason}"

    @beta_tool(eager_input_streaming=True)
    def delete_source(source_id: int) -> str:
        """Delete a cached source, e.g. when the user says it's wrong or unreliable.

        Args:
            source_id: The id of the source to delete.
        """
        return "Deleted." if sources.delete(source_id) else f"No source with id {source_id}."

    tools = [find_sources, http_get, save_source, delete_source]
    if search is not None:
        tools.insert(1, web_search)
    return tools, sources
