"""TopGenie: a Streamlit app on the Databricks Genie Conversation API."""

from __future__ import annotations

import io
import os
import time
import types
import urllib.request

import pandas as pd
import plotly.express as px
import pyarrow as pa
import pyarrow.ipc as ipc
import sqlparse
import streamlit as st
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementState
from streamlit_ace import st_ace

PALETTE = px.colors.sequential.Plasma_r
# Use string status checks for Genie responses (we bypass the SDK there to avoid
# schema-drift KeyErrors during deserialization).
TERMINAL_MSG = {"COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}
TERMINAL_STMT = {
    StatementState.SUCCEEDED,
    StatementState.FAILED,
    StatementState.CANCELED,
    StatementState.CLOSED,
}
NUMERIC_TYPES = {
    "INT",
    "INTEGER",
    "LONG",
    "BIGINT",
    "SHORT",
    "SMALLINT",
    "BYTE",
    "TINYINT",
    "DOUBLE",
    "FLOAT",
    "DECIMAL",
    "NUMERIC",
}
FAILED_HINTS = (
    "**Common causes of FAILED:**\n\n"
    "1. **Missing data grants.** The SP needs `USE SCHEMA` + `SELECT` on the schema this space queries.\n"
    "2. **Question off-schema.** Genie cannot answer outside the curated tables.\n"
    "3. **SQL runtime error.** See the SQL above; tail `<app-url>/logz` for the warehouse error."
)

PINNED_SPACE_ID = os.environ.get("GENIE_SPACE_ID")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID")

st.set_page_config(page_title="TopGenie", page_icon=":bar_chart:", layout="wide")

st.markdown(
    """
<style>
/* Gradient app background */
.stApp {
    background: radial-gradient(1200px 600px at 10% -10%, #EDE9FE 0%, transparent 50%),
                radial-gradient(1000px 500px at 90% 110%, #DBEAFE 0%, transparent 50%),
                #F7F7FA;
}
/* Glassy sidebar */
section[data-testid="stSidebar"] {
    background: rgba(255, 255, 255, 0.55);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-right: 1px solid rgba(255, 255, 255, 0.4);
}
section[data-testid="stSidebar"] h3 {
    font-weight: 600;
    letter-spacing: -0.01em;
    color: #1F2937;
}
/* Chat bubbles: soft rounded cards */
[data-testid="stChatMessage"] {
    background: rgba(255, 255, 255, 0.7);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    border: 1px solid rgba(229, 231, 235, 0.6);
    border-radius: 16px;
    padding: 14px 18px;
    margin-bottom: 14px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}
/* Code blocks: cleaner mono */
.stCode, pre code, [data-testid="stCodeBlock"] {
    border-radius: 12px;
    font-size: 13px;
}
/* Buttons: subtle elevation */
.stButton > button, .stDownloadButton > button {
    border-radius: 10px;
    border: 1px solid rgba(124, 58, 237, 0.2);
    transition: all 0.15s ease;
}
.stButton > button:hover, .stDownloadButton > button:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(124, 58, 237, 0.15);
    border-color: rgba(124, 58, 237, 0.4);
}
/* Chat input: glass dock */
[data-testid="stChatInput"] {
    background: rgba(255, 255, 255, 0.75);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-radius: 14px;
    border: 1px solid rgba(229, 231, 235, 0.7);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.04);
}
/* Title: tighter, slightly graphic */
h1 {
    font-weight: 700;
    letter-spacing: -0.025em;
    background: linear-gradient(135deg, #1F2937 0%, #7C3AED 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}
/* Tables: rounded */
[data-testid="stDataFrame"] {
    border-radius: 12px;
    overflow: hidden;
}
/* Expander: glass-card */
[data-testid="stExpander"] {
    background: rgba(255, 255, 255, 0.55);
    border-radius: 12px;
    border: 1px solid rgba(229, 231, 235, 0.5);
}
/* Caption / muted text */
[data-testid="stCaptionContainer"] {
    color: #6B7280;
}
</style>
""",
    unsafe_allow_html=True,
)

st.title("TopGenie")


@st.cache_resource
def get_client() -> WorkspaceClient:
    if os.environ.get("DATABRICKS_APP_NAME"):
        return WorkspaceClient()
    return WorkspaceClient(profile=os.environ.get("DATABRICKS_PROFILE", "DEFAULT"))


@st.cache_data(ttl=60)
def list_spaces() -> list[tuple[str, str]]:
    try:
        return [(s.space_id, s.title or s.space_id) for s in (get_client().genie.list_spaces().spaces or [])]
    except Exception:
        return []


def _to_ns(d):
    """Convert nested dicts/lists into SimpleNamespace for attribute access."""
    if isinstance(d, dict):
        return types.SimpleNamespace(**{k: _to_ns(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_to_ns(x) for x in d]
    return d


def _genie_call(method: str, path: str, body: dict | None = None):
    """Call a Genie REST endpoint and return a namespace object (or raise)."""
    raw = get_client().api_client.do(method, path, body=body or {})
    return _to_ns(raw)


def get_warehouse_id(space_id: str) -> str:
    if WAREHOUSE_ID:
        return WAREHOUSE_ID
    space = _genie_call("GET", f"/api/2.0/genie/spaces/{space_id}")
    return space.warehouse_id


def format_sql(sql: str) -> str:
    try:
        return sqlparse.format(sql or "", reindent=True, keyword_case="upper") if sql else ""
    except Exception:
        return sql


def _coerce_types(df: pd.DataFrame, cols) -> pd.DataFrame:
    for c in cols:
        t = (c.type_name.value if hasattr(c.type_name, "value") else str(c.type_name)).upper()
        try:
            if t in NUMERIC_TYPES:
                df[c.name] = pd.to_numeric(df[c.name], errors="coerce")
            elif t == "DATE":
                df[c.name] = pd.to_datetime(df[c.name], errors="coerce").dt.date
            elif t in ("TIMESTAMP", "TIMESTAMP_NTZ"):
                df[c.name] = pd.to_datetime(df[c.name], errors="coerce")
            elif t in ("BOOLEAN", "BOOL"):
                df[c.name] = df[c.name].astype(str).str.lower().isin(("true", "1", "t"))
        except Exception:
            pass
    return df


def result_to_df(sr) -> pd.DataFrame:
    manifest = getattr(sr, "manifest", None)
    schema = getattr(manifest, "schema", None) if manifest else None
    if not schema:
        return pd.DataFrame()
    cols = getattr(schema, "columns", None) or []
    result = getattr(sr, "result", None)
    rows = (getattr(result, "data_array", None) if result else None) or []
    return _coerce_types(pd.DataFrame(rows, columns=[c.name for c in cols]), cols)


def with_cap(sql: str, max_rows: int) -> str:
    """Wrap a SELECT/WITH query in an outer LIMIT. Non-SELECT statements pass through."""
    stripped = (sql or "").strip().rstrip(";").strip()
    if not stripped.upper().lstrip("(").startswith(("SELECT", "WITH")):
        return sql
    return f"SELECT * FROM (\n{stripped}\n) AS topgenie_capped LIMIT {int(max_rows)}"


def _stmt_error(resp) -> str:
    return resp.status.error.message if resp.status.error else str(resp.status.state)


def _poll_to_terminal(w, resp):
    deadline = time.time() + 180
    while resp.status.state not in TERMINAL_STMT and time.time() < deadline:
        time.sleep(1.0)
        resp = w.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"Statement failed: {_stmt_error(resp)}")
    return resp


def _fetch_inline_paginated(sql: str, warehouse_id: str) -> pd.DataFrame:
    """Run SQL with INLINE+JSON and page through chunks via the control plane.

    Used as a fallback when the app's network can't reach presigned cloud-storage URLs
    (restricted-egress Databricks Apps). Slower bytes-on-the-wire than ARROW_STREAM
    but never leaves the workspace.
    """
    w = get_client()
    resp = _poll_to_terminal(
        w,
        w.statement_execution.execute_statement(
            statement=sql,
            warehouse_id=warehouse_id,
            disposition=Disposition.INLINE,
            format=Format.JSON_ARRAY,
            wait_timeout="50s",
        ),
    )
    cols = (
        getattr(getattr(resp.manifest, "schema", None), "columns", None)
        if getattr(resp, "manifest", None)
        else None
    ) or []
    rows = list((resp.result.data_array if resp.result else None) or [])
    next_idx = getattr(resp.result, "next_chunk_index", None) if resp.result else None
    while next_idx is not None:
        chunk = w.statement_execution.get_statement_result_chunk_n(
            statement_id=resp.statement_id,
            chunk_index=next_idx,
        )
        rows.extend(chunk.data_array or [])
        next_idx = getattr(chunk, "next_chunk_index", None)
    return _coerce_types(pd.DataFrame(rows, columns=[c.name for c in cols]), cols)


def execute_external_links(sql: str, warehouse_id: str) -> pd.DataFrame:
    """Execute SQL and return the full result.

    Primary path: EXTERNAL_LINKS + ARROW_STREAM (fastest, bypasses inline 25 MB cap).
    Fallback: INLINE + JSON paginated via control plane, used when the app can't
    reach presigned cloud-storage URLs (e.g. restricted-egress workspaces).
    """
    w = get_client()
    resp = _poll_to_terminal(
        w,
        w.statement_execution.execute_statement(
            statement=sql,
            warehouse_id=warehouse_id,
            disposition=Disposition.EXTERNAL_LINKS,
            format=Format.ARROW_STREAM,
            wait_timeout="50s",
        ),
    )
    links = (resp.result.external_links if resp.result else None) or []
    if not links:
        return pd.DataFrame()
    try:
        tables = []
        for link in links:
            with urllib.request.urlopen(link.external_link, timeout=30) as r:
                tables.append(ipc.open_stream(io.BytesIO(r.read())).read_all())
        return pa.concat_tables(tables).to_pandas()
    except (urllib.error.URLError, OSError) as e:
        st.info(f"External links unreachable ({type(e).__name__}); falling back to paginated inline fetch.")
        return _fetch_inline_paginated(sql, warehouse_id)


def run_inline(sql: str, warehouse_id: str, row_cap: int) -> tuple[pd.DataFrame, str | None]:
    """Execute SQL via Statement Execution with an outer LIMIT cap; return (df, truncated_or_None)."""
    resp = get_client().statement_execution.execute_statement(
        statement=with_cap(sql, row_cap),
        warehouse_id=warehouse_id,
        wait_timeout="30s",
    )
    if resp.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(_stmt_error(resp))
    df = result_to_df(resp)
    return df, ("app_cap" if len(df) >= row_cap else None)


def _message_id(obj) -> str | None:
    """Read a message id from a Genie response across schema variants."""
    if obj is None:
        return None
    return (
        getattr(obj, "id", None)
        or getattr(obj, "message_id", None)
        or (getattr(obj.message, "id", None) if getattr(obj, "message", None) else None)
    )


STAGE_LABELS = {
    "IN_PROGRESS": "Genie is reading the schema...",
    "EXECUTING_QUERY": "Query running on warehouse...",
    "FETCHING_METADATA": "Materializing the result...",
}


def ask_genie_live(space_id: str, question: str, conversation_id: str | None, status_ph, sql_ph):
    """Send a question to Genie and poll to completion, updating UI placeholders live.

    `status_ph` shows the current stage + elapsed time. `sql_ph` shows the generated
    SQL the moment it becomes available, before the warehouse finishes executing it.
    """
    if conversation_id:
        path = f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages"
    else:
        path = f"/api/2.0/genie/spaces/{space_id}/start-conversation"
    started = time.time()
    status_ph.markdown("**Sending question to Genie...**  `0.0s`")
    p = _genie_call("POST", path, body={"content": question})
    cid = getattr(p, "conversation_id", conversation_id) or conversation_id
    mid = _message_id(p)
    if not (cid and mid):
        raise RuntimeError(f"Genie did not return ids (conversation={cid!r}, message={mid!r})")

    deadline = time.time() + 180
    msg = None
    rendered_sql = False
    while time.time() < deadline:
        msg = _genie_call("GET", f"/api/2.0/genie/spaces/{space_id}/conversations/{cid}/messages/{mid}")
        status = getattr(msg, "status", "")
        attachments = getattr(msg, "attachments", None) or []
        qa = next(
            (a for a in attachments if getattr(a, "query", None) and getattr(a.query, "query", None)), None
        )

        elapsed = time.time() - started
        if qa and status not in TERMINAL_MSG:
            label = "Genie wrote the SQL, query running..."
        else:
            label = STAGE_LABELS.get(status, f"Working ({status or 'queued'})...")
        status_ph.markdown(f"**{label}**  `{elapsed:.1f}s`")

        if qa and not rendered_sql:
            with sql_ph.container():
                desc = getattr(qa.query, "description", None)
                if desc:
                    st.caption(desc)
                st.code(format_sql(qa.query.query), language="sql")
            rendered_sql = True

        if status in TERMINAL_MSG:
            break

        time.sleep(0.3 if elapsed < 5 else 1.5)
    return msg


def error_details(msg) -> str:
    parts = []
    if err := getattr(msg, "error", None):
        parts.append(getattr(err, "message", None) or str(err))
    for a in getattr(msg, "attachments", None) or []:
        text = getattr(getattr(a, "text", None), "content", None)
        if text:
            parts.append(text)
        sql = getattr(getattr(a, "query", None), "query", None)
        if sql:
            parts.append(f"SQL Genie tried:\n```sql\n{sql}\n```")
    return "\n\n".join(parts) or "(no additional details from the API)"


def fetch_result(space_id: str, msg, mode: str, row_cap: int) -> tuple[pd.DataFrame | None, str | None]:
    """Return (df, truncated_reason) for a Genie message; branch on inline vs external_links."""
    attachments = getattr(msg, "attachments", None) or []
    qa = next((a for a in attachments if getattr(a, "query", None)), None)
    if not qa:
        return None, None
    if mode == "external_links":
        return execute_external_links(qa.query.query, get_warehouse_id(space_id)), None

    mid = _message_id(msg)
    path = (
        f"/api/2.0/genie/spaces/{space_id}/conversations/{msg.conversation_id}"
        f"/messages/{mid}/attachments/{qa.attachment_id}/query-result"
    )
    sr = _genie_call("GET", path).statement_response
    state = getattr(getattr(sr, "status", None), "state", "")
    if state != "SUCCEEDED":
        st.error(f"Query did not succeed: {state}")
        return None, None

    df = result_to_df(sr)
    if len(df) > row_cap:
        return df.head(row_cap), "app_cap"
    if getattr(sr, "result", None) and getattr(sr.result, "next_chunk_internal_link", None):
        return df, "server"
    return df, None


def render_chart(df: pd.DataFrame, key: str):
    if df.empty:
        return
    num = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cat = [c for c in df.columns if c not in num]
    tcols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    chart_types = ["Bar", "Line", "Area", "Scatter", "None"]
    default = 1 if (tcols and num) else 3 if (len(num) >= 2 and not cat) else 0

    c1, c2, c3 = st.columns(3)
    chart = c1.selectbox("Chart type", chart_types, index=default, key=f"ct_{key}")
    if chart == "None":
        return
    cols = df.columns.tolist()
    x = c2.selectbox("X axis", cols, index=cols.index((tcols + cat + num)[0]), key=f"x_{key}")
    yopts = num or cols
    y = c3.selectbox(
        "Y axis", yopts, index=yopts.index(num[0]) if num and num[0] in yopts else 0, key=f"y_{key}"
    )

    plot = df[[x, y]].dropna()
    try:
        if chart == "Bar":
            plot = plot.sort_values(y, ascending=False)
            fig = px.bar(
                plot, x=x, y=y, color=y, color_continuous_scale=PALETTE, hover_data={x: True, y: ":,.2f"}
            )
            fig.update_layout(coloraxis_showscale=False)
        elif chart == "Line":
            fig = px.line(plot, x=x, y=y, markers=True)
            fig.update_traces(line=dict(width=2.5, color="#9C27B0"), marker=dict(size=8))
        elif chart == "Area":
            fig = px.area(plot, x=x, y=y)
            fig.update_traces(line=dict(width=2.5, color="#9C27B0"), fillcolor="rgba(156, 39, 176, 0.25)")
        else:
            fig = px.scatter(plot, x=x, y=y, color=y, color_continuous_scale=PALETTE)
            fig.update_traces(marker=dict(size=10, line=dict(width=1, color="white")))
            fig.update_layout(coloraxis_showscale=False)

        fig.update_layout(
            plot_bgcolor="#fafafa",
            paper_bgcolor="#fafafa",
            font=dict(family="Inter, sans-serif", size=12, color="#333"),
            margin=dict(l=40, r=20, t=30, b=40),
            hoverlabel=dict(bgcolor="white", font_size=12),
            xaxis=dict(showgrid=False, linecolor="#bbb"),
            yaxis=dict(gridcolor="#e5e5e5", linecolor="#bbb"),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})
    except Exception as e:
        st.warning(f"Could not render chart: {e}")


# Session state
st.session_state.setdefault("history", [])
st.session_state.setdefault("conversation_id", None)
st.session_state.setdefault("space_id", PINNED_SPACE_ID)

# Sidebar
with st.sidebar:
    st.subheader("Genie space")
    if PINNED_SPACE_ID:
        st.code(PINNED_SPACE_ID, language=None)
        st.caption("Pinned via `GENIE_SPACE_ID`.")
    else:
        spaces = list_spaces()
        if spaces:
            labels = {f"{title} ({sid[:8]}...)": sid for sid, title in spaces}
            keys = list(labels.keys())
            current = next((k for k, v in labels.items() if v == st.session_state.space_id), keys[0])
            choice = st.selectbox("Pick a space", keys, index=keys.index(current))
            if labels[choice] != st.session_state.space_id:
                st.session_state.update(space_id=labels[choice], history=[], conversation_id=None)
                st.rerun()
        else:
            manual = st.text_input("Genie space ID", value=st.session_state.space_id or "")
            if manual:
                st.session_state.space_id = manual

    st.divider()
    st.subheader("Conversation")
    if st.session_state.conversation_id:
        st.code(st.session_state.conversation_id, language=None)
    else:
        st.write("No conversation yet.")
    if st.button("New conversation"):
        st.session_state.update(history=[], conversation_id=None)
        st.rerun()
    st.divider()
    with st.expander("Expert", expanded=False):
        st.markdown("**Result fetch**")
        st.caption(
            "How TopGenie pulls the result rows back from the warehouse after "
            "Genie generates the SQL. Pick **Inline + cap** for fast charting and "
            "previews, **External links** for the full result."
        )
        fetch_mode_label = st.radio(
            "Mode",
            ["Inline + cap", "External links"],
            captions=[
                "Fast. Fetches one chunk, truncates to the row cap below. Best for exploration and charts.",
                "Full result, no cap. Costs one extra warehouse call. "
                "Falls back to paginated inline if cloud storage egress is blocked.",
            ],
            index=0,
            key="fetch_mode",
        )
        ROW_CAP = st.slider(
            "Row cap (inline only)",
            min_value=10,
            max_value=10_000,
            value=2_000,
            step=10,
            disabled=(fetch_mode_label != "Inline + cap"),
            help="Maximum rows kept in app memory in Inline mode. "
            "If the warning bar shows the result was truncated, raise this or "
            "click 'Load full via External Links' on the turn.",
            key="row_cap",
        )
        st.divider()
        st.markdown("**About**")
        st.markdown(
            "- Calls the **Genie Conversation API**\n"
            "- Shows the **generated SQL** (editable + rerun)\n"
            "- Renders a **chart** from the result\n"
            "- **Download** as CSV"
        )
    FETCH_MODE = "external_links" if fetch_mode_label == "External links" else "inline"

SPACE_ID = st.session_state.space_id
if not SPACE_ID:
    st.info("Pick a Genie space in the sidebar to start.")
    st.stop()
st.caption(f"Genie space: `{SPACE_ID}`. Ask a question, see the SQL, the chart, and download the result.")

# Ask: append a pending turn and rerun so the user bubble appears immediately
prompt = st.chat_input("Ask Genie about the data...")
if prompt:
    st.session_state.history.append(
        {
            "question": prompt,
            "pending": True,
            "sql": None,
            "raw_sql": None,
            "description": None,
            "text": None,
            "df": None,
            "truncated": None,
        }
    )
    st.rerun()


def _drive_pending(turn):
    """Drive a pending turn: live status + streamed SQL + result fetch, then rerun."""
    status_ph = st.empty()
    sql_ph = st.empty()
    try:
        msg = ask_genie_live(SPACE_ID, turn["question"], st.session_state.conversation_id, status_ph, sql_ph)
        st.session_state.conversation_id = (
            getattr(msg, "conversation_id", None) or st.session_state.conversation_id
        )
        status = getattr(msg, "status", "")
        if status != "COMPLETED":
            status_ph.error(f"Genie returned status: **{status}**")
            st.markdown(error_details(msg))
            if status == "FAILED":
                st.info(FAILED_HINTS)
            turn.update(pending=False, error=f"Genie returned status: {status}")
            return
        attachments = getattr(msg, "attachments", None) or []
        qa = next((a for a in attachments if getattr(a, "query", None)), None)
        ta = next((a for a in attachments if getattr(a, "text", None)), None)
        status_ph.markdown("**Loading rows...**")
        df, truncated = fetch_result(SPACE_ID, msg, FETCH_MODE, ROW_CAP) if qa else (None, None)
        turn.update(
            pending=False,
            sql=format_sql(qa.query.query) if qa else None,
            raw_sql=qa.query.query if qa else None,
            description=getattr(qa.query, "description", None) if qa else None,
            text=ta.text.content if ta else None,
            df=df,
            truncated=truncated,
        )
        st.rerun()
    except Exception as e:
        status_ph.error(f"Genie call failed: {type(e).__name__}: {e}")
        turn.update(pending=False, error=f"{type(e).__name__}: {e}")


# History
for i, turn in enumerate(st.session_state.history):
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        if turn.get("pending"):
            _drive_pending(turn)
            continue
        if turn.get("error"):
            st.error(turn["error"])
        if turn.get("text"):
            st.markdown(turn["text"])
        if turn.get("description"):
            st.caption(turn["description"])

        if turn.get("sql"):
            with st.expander("Edit and rerun SQL", expanded=False):
                edited = st_ace(
                    value=turn["sql"],
                    language="sql",
                    theme="github",
                    keybinding="vscode",
                    font_size=13,
                    tab_size=2,
                    wrap=True,
                    show_print_margin=False,
                    auto_update=True,
                    min_lines=8,
                    key=f"sql_{i}",
                )
                if st.button("Rerun edited SQL", key=f"rerun_{i}"):
                    try:
                        wh = get_warehouse_id(SPACE_ID)
                        if FETCH_MODE == "external_links":
                            new_df, new_trunc = execute_external_links(edited, wh), None
                        else:
                            new_df, new_trunc = run_inline(edited, wh, ROW_CAP)
                        turn.update(df=new_df, sql=format_sql(edited), raw_sql=edited, truncated=new_trunc)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Statement execution failed: {e}")

        if turn.get("truncated") and turn.get("raw_sql"):
            row_count = len(turn["df"]) if isinstance(turn.get("df"), pd.DataFrame) else 0
            label = (
                f"Server truncated the inline chunk (~25 MB). Showing {row_count} rows."
                if turn["truncated"] == "server"
                else f"Truncated to in-app cap. Showing {row_count} rows (current cap: {ROW_CAP})."
            )
            st.warning(label)
            c1, c2 = st.columns(2)
            if c1.button("Re-fetch with current cap", key=f"recap_{i}"):
                try:
                    new_df, new_trunc = run_inline(turn["raw_sql"], get_warehouse_id(SPACE_ID), ROW_CAP)
                    turn.update(df=new_df, truncated=new_trunc)
                    st.rerun()
                except Exception as e:
                    st.error(f"Re-fetch failed: {e}")
            if c2.button("Load full via External Links", key=f"escalate_{i}"):
                try:
                    new_df = execute_external_links(turn["raw_sql"], get_warehouse_id(SPACE_ID))
                    turn.update(df=new_df, truncated=None)
                    st.rerun()
                except Exception as e:
                    st.error(f"External Links fetch failed: {e}")

        df = turn.get("df")
        if isinstance(df, pd.DataFrame) and not df.empty:
            tab_table, tab_chart = st.tabs(["Table", "Chart"])
            with tab_table:
                st.dataframe(df, use_container_width=True)
                st.download_button(
                    "Download CSV",
                    data=df.to_csv(index=False).encode("utf-8"),
                    file_name=f"genie_result_{i + 1}.csv",
                    mime="text/csv",
                    key=f"dl_{i}",
                )
            with tab_chart:
                render_chart(df, key=str(i))
        elif df is not None:
            st.info("Query returned no rows.")
