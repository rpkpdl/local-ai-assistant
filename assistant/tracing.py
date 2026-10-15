"""Optional tracing to a local Arize Phoenix server (OpenTelemetry / OpenInference).

Enabled when PHOENIX_COLLECTOR_ENDPOINT is set (e.g. in .env). Produces, per turn:
  turn (agent span)
  ├── beta.messages.parse   (LLM spans: tokens, cost, prompts - auto-instrumented)
  ├── <tool name>           (tool spans: input/output)
  │   └── db SELECT sources (SQLite spans: every query, without parameter values)
  └── ...
Turns from one REPL run are grouped into a Phoenix session. PHOENIX_PROJECT_NAME picks the
Phoenix project (evals use "personal-assistant-evals", so they don't mix with real use).
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


def setup_tracing(project_name: str | None = None) -> bool:
    """Call before opening SQLite connections. Returns True if tracing is on. Safe to call again."""
    global _tracer
    if _tracer is not None:
        return True
    if not os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"):
        return False
    from phoenix.otel import register

    project_name = project_name or os.getenv("PHOENIX_PROJECT_NAME") or "personal-assistant"
    # auto_instrument picks up openinference-instrumentation-anthropic
    provider = register(project_name=project_name, auto_instrument=True, batch=True,
                        verbose=False)
    _tracer = provider.get_tracer("assistant")
    # Every sqlite3.connect() from here on returns a TracedConnection
    sqlite3.connect = functools.partial(sqlite3.connect, factory=TracedConnection)
    return True


class TracedConnection(sqlite3.Connection):
    """Records a span per query: the SQL text and row count, never parameter values.

    Only inside another span (a turn or a tool). Queries outside one, like startup schema
    setup or eval seeding, would each become a separate trace and bury the LLM calls.
    """

    def execute(self, sql, parameters=(), /):
        if not _in_span():
            return super().execute(sql, parameters)
        with _db_span(sql) as span:
            cur = super().execute(sql, parameters)
            span.set_attribute("db.rows_affected", cur.rowcount)
            return cur

    def executescript(self, sql_script, /):
        if not _in_span():
            return super().executescript(sql_script)
        with _db_span(sql_script):
            return super().executescript(sql_script)


def _in_span() -> bool:
    from opentelemetry import trace
    return trace.get_current_span().get_span_context().is_valid


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


def in_caller_context(fn):
    """Wrap fn so it runs in the current trace context, even on another thread.

    Agent turns run on a worker thread; without this, a turn started inside a span (an eval
    case) would become its own trace instead of nesting under it.
    """
    if _tracer is None:
        return fn
    from opentelemetry import context

    ctx = context.get_current()

    def run(*args, **kwargs):
        token = context.attach(ctx)
        try:
            return fn(*args, **kwargs)
        finally:
            context.detach(token)
    return run


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


def case_span(name: str, **metadata):
    """Group one eval case's LLM calls under a CHAIN span, in the run's Phoenix session.

    The eval runner sets SESSION_ID to the run's name, so each run's cost shows up as one
    session. No-op when tracing is off.
    """
    if _tracer is None:
        return contextlib.nullcontext()
    import json

    from openinference.instrumentation import using_session

    stack = contextlib.ExitStack()
    stack.enter_context(using_session(SESSION_ID))
    span = stack.enter_context(_tracer.start_as_current_span(
        name, openinference_span_kind="chain"))
    span.set_attribute("metadata", json.dumps(metadata, default=str))
    return stack


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
