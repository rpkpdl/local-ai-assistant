"""Optional tracing to a local Arize Phoenix server (OpenTelemetry / OpenInference).

Enabled when PHOENIX_COLLECTOR_ENDPOINT is set (e.g. in .env). Produces, per turn:
  turn (agent span)
  ├── beta.messages.parse   (LLM spans: tokens, cost, prompts - auto-instrumented)
  ├── <tool name>           (tool spans: input/output)
  │   └── db SELECT sources (SQLite spans: every query, without parameter values)
  └── ...
Turns from one REPL run are grouped into a Phoenix session.
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import sqlite3
import uuid

_tracer = None
SESSION_ID = uuid.uuid4().hex


def setup_tracing(project_name: str = "personal-assistant") -> bool:
    """Call before opening SQLite connections. Returns True if tracing is on."""
    global _tracer
    if not os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"):
        return False
    from phoenix.otel import register

    # auto_instrument picks up openinference-instrumentation-anthropic
    provider = register(project_name=project_name, auto_instrument=True, batch=True,
                        verbose=False)
    _tracer = provider.get_tracer("assistant")
    # Every sqlite3.connect() from here on returns a TracedConnection
    sqlite3.connect = functools.partial(sqlite3.connect, factory=TracedConnection)
    return True


class TracedConnection(sqlite3.Connection):
    """Records a span per query: the SQL text and row count, never parameter values."""

    def execute(self, sql, parameters=(), /):
        with _db_span(sql) as span:
            cur = super().execute(sql, parameters)
            span.set_attribute("db.rows_affected", cur.rowcount)
            return cur

    def executescript(self, sql_script, /):
        with _db_span(sql_script):
            return super().executescript(sql_script)


def _db_span(sql: str):
    words = sql.split()
    verb = words[0].upper() if words else "SQL"
    m = re.search(r"\b(?:FROM|INTO|UPDATE|TABLE(?: IF NOT EXISTS)?|EXISTS)\s+(\w+)", sql, re.I)
    name = f"db {verb} {m.group(1)}" if m else f"db {verb}"
    span = _tracer.start_as_current_span(name)
    ctx = span.__enter__()
    ctx.set_attribute("db.system", "sqlite")
    ctx.set_attribute("db.statement", " ".join(words))

    @contextlib.contextmanager
    def wrapper():
        try:
            yield ctx
        except BaseException as e:
            span.__exit__(type(e), e, e.__traceback__)
            raise
        else:
            span.__exit__(None, None, None)
    return wrapper()


def turn_span(user_text: str):
    """Context manager wrapping one user turn; yields a span (or None when off)."""
    if _tracer is None:
        return contextlib.nullcontext()
    from openinference.instrumentation import using_session

    stack = contextlib.ExitStack()
    stack.enter_context(using_session(SESSION_ID))
    span = stack.enter_context(_tracer.start_as_current_span(
        "turn", openinference_span_kind="agent"))
    span.set_input(user_text)

    @contextlib.contextmanager
    def ctx():
        with stack:
            yield span
    return ctx()


def trace_tools(tools: list) -> list:
    """Wrap each tool's call() in a TOOL span. No-op when tracing is off."""
    if _tracer is None:
        return tools
    for tool in tools:
        tool.call = _wrap(tool, tool.call)
    return tools


def _wrap(tool, original):
    schema = tool.to_dict()

    def call(input):
        with _tracer.start_as_current_span(tool.name, openinference_span_kind="tool") as span:
            span.set_tool(name=tool.name, description=schema.get("description"),
                          parameters=schema.get("input_schema", {}))
            span.set_input(input)
            result = original(input)
            span.set_output(result)
            return result

    return call
