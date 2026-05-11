"""TopGenie: a Streamlit app on the Databricks Genie Conversation API.

Asks a Genie space a question, shows the generated SQL (editable + rerun),
renders a chart from the result, and offers a CSV download.

Configure with two environment variables:
    GENIE_SPACE_ID         -- the Genie space to query
    DATABRICKS_WAREHOUSE_ID -- the warehouse used for "Rerun edited SQL"
"""
from __future__ import annotations

import os
import time

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
import sqlparse
import streamlit as st
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.dashboards import MessageStatus
from databricks.sdk.service.sql import StatementState

TERMINAL_STATUSES = {
    MessageStatus.COMPLETED,
    MessageStatus.FAILED,
    MessageStatus.CANCELLED,
    MessageStatus.QUERY_RESULT_EXPIRED,
}


def format_sql(sql: str) -> str:
    if not sql:
        return ""
    try:
        return sqlparse.format(sql, reindent=True, keyword_case="upper", strip_comments=False)
    except Exception:
        return sql

PINNED_SPACE_ID = os.environ.get("GENIE_SPACE_ID")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID")

st.set_page_config(page_title="TopGenie", page_icon=":bar_chart:", layout="wide")
st.title("TopGenie")


@st.cache_resource
def get_client() -> WorkspaceClient:
    if os.environ.get("DATABRICKS_APP_NAME"):
        return WorkspaceClient()
    return WorkspaceClient(profile=os.environ.get("DATABRICKS_PROFILE", "DEFAULT"))


@st.cache_data(ttl=60)
def list_spaces() -> list[tuple[str, str]]:
    w = get_client()
    try:
        return [(s.space_id, s.title or s.space_id) for s in (w.genie.list_spaces().spaces or [])]
    except Exception:
        return []


def get_warehouse_id(space_id: str) -> str:
    if WAREHOUSE_ID:
        return WAREHOUSE_ID
    return get_client().genie.get_space(space_id=space_id).warehouse_id


def _coerce(v, type_name: str):
    if v is None:
        return None
    t = (type_name or "").upper()
    try:
        if t in ("INT", "INTEGER", "LONG", "BIGINT", "SHORT", "SMALLINT", "BYTE", "TINYINT"):
            return int(float(v))
        if t in ("DOUBLE", "FLOAT", "DECIMAL", "NUMERIC"):
            return float(v)
        if t in ("BOOLEAN", "BOOL"):
            return str(v).lower() in ("true", "1", "t")
        if t == "DATE":
            return pd.to_datetime(v).date()
        if t in ("TIMESTAMP", "TIMESTAMP_NTZ"):
            return pd.to_datetime(v)
    except Exception:
        return v
    return v


def result_to_df(statement_response) -> pd.DataFrame:
    if not statement_response.manifest or not statement_response.manifest.schema:
        return pd.DataFrame()
    cols = statement_response.manifest.schema.columns
    names = [c.name for c in cols]
    types = [c.type_name.value if hasattr(c.type_name, "value") else str(c.type_name) for c in cols]
    rows = (statement_response.result.data_array if statement_response.result else None) or []
    coerced = [[_coerce(v, t) for v, t in zip(row, types, strict=False)] for row in rows]
    return pd.DataFrame(coerced, columns=names)


def _poll_message(space_id: str, conversation_id: str, message_id: str, timeout_s: int = 180):
    w = get_client()
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = w.genie.get_message(
            space_id=space_id, conversation_id=conversation_id, message_id=message_id
        )
        if last.status in TERMINAL_STATUSES:
            return last
        time.sleep(1.5)
    return last


def ask_genie(space_id: str, question: str, conversation_id: str | None):
    """Start or continue a Genie conversation and poll to a terminal status.

    Returns the GenieMessage regardless of success or failure; caller inspects
    `msg.status` and renders details on FAILED.
    """
    w = get_client()
    if conversation_id:
        partial = w.genie.create_message(
            space_id=space_id, conversation_id=conversation_id, content=question
        )
        cid = partial.conversation_id or conversation_id
        mid = partial.id or partial.message_id
    else:
        partial = w.genie.start_conversation(space_id=space_id, content=question)
        cid = partial.conversation_id
        mid = partial.message_id or (partial.message.id if partial.message else None)
    return _poll_message(space_id, cid, mid)


def _extract_error_details(msg) -> str:
    """Pull human-readable error context out of a non-COMPLETED message."""
    parts = []
    err = getattr(msg, "error", None)
    if err:
        parts.append(getattr(err, "message", None) or str(err))
    for a in (msg.attachments or []):
        if a.text and a.text.content:
            parts.append(a.text.content)
        if a.query and a.query.query:
            parts.append(f"SQL Genie tried:\n```sql\n{a.query.query}\n```")
    return "\n\n".join(p for p in parts if p) or "(no additional details from the API)"


def fetch_result(space_id: str, msg) -> pd.DataFrame | None:
    qa = next((a for a in (msg.attachments or []) if a.query), None)
    if not qa:
        return None
    w = get_client()
    r = w.genie.get_message_attachment_query_result(
        space_id=space_id,
        conversation_id=msg.conversation_id,
        message_id=msg.message_id or msg.id,
        attachment_id=qa.attachment_id,
    )
    sr = r.statement_response
    if sr.status.state != StatementState.SUCCEEDED:
        st.error(f"Query did not succeed: {sr.status.state}")
        return None
    return result_to_df(sr)


sns.set_theme(style="whitegrid", context="talk", palette="rocket")


def _format_axis(ax, x_label, y_label, title=None):
    ax.set_xlabel(x_label, fontsize=11, color="#444")
    ax.set_ylabel(y_label, fontsize=11, color="#444")
    if title:
        ax.set_title(title, fontsize=13, weight="bold", pad=12)
    ax.tick_params(axis="x", labelsize=9, rotation=30)
    ax.tick_params(axis="y", labelsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#bbb")
    ax.yaxis.grid(True, linestyle=":", linewidth=0.6, color="#ccc")
    ax.xaxis.grid(False)


def render_chart(df: pd.DataFrame, key: str):
    if df.empty:
        return
    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cat_cols = [c for c in df.columns if c not in num_cols]
    time_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]

    chart_types = ["Bar", "Line", "Area", "Scatter", "None"]
    default_idx = 0
    if time_cols and num_cols:
        default_idx = 1
    elif len(num_cols) >= 2 and not cat_cols:
        default_idx = 3

    c1, c2, c3 = st.columns([1, 1, 1])
    chart_type = c1.selectbox("Chart type", chart_types, index=default_idx, key=f"ct_{key}")
    if chart_type == "None":
        return

    x_default = (time_cols + cat_cols + num_cols)[0]
    y_default = num_cols[0] if num_cols else df.columns[0]
    x = c2.selectbox("X axis", df.columns.tolist(), index=df.columns.tolist().index(x_default), key=f"x_{key}")
    y_options = num_cols or df.columns.tolist()
    y = c3.selectbox("Y axis", y_options, index=y_options.index(y_default) if y_default in y_options else 0, key=f"y_{key}")

    try:
        plot_df = df[[x, y]].dropna()
        fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
        fig.patch.set_facecolor("#fafafa")
        ax.set_facecolor("#fafafa")

        if chart_type == "Bar":
            order = plot_df.sort_values(y, ascending=False)[x].tolist()
            sns.barplot(data=plot_df, x=x, y=y, ax=ax, order=order, hue=x, palette="rocket", legend=False)
        elif chart_type == "Line":
            sns.lineplot(data=plot_df, x=x, y=y, ax=ax, marker="o", linewidth=2.2, color="#9C27B0")
        elif chart_type == "Area":
            sns.lineplot(data=plot_df, x=x, y=y, ax=ax, linewidth=2.2, color="#9C27B0")
            ax.fill_between(plot_df[x], plot_df[y], alpha=0.25, color="#9C27B0")
        elif chart_type == "Scatter":
            sns.scatterplot(data=plot_df, x=x, y=y, ax=ax, s=70, color="#E91E63", edgecolor="white")

        _format_axis(ax, x, y)
        fig.tight_layout()
        st.pyplot(fig, use_container_width=True)
        plt.close(fig)
    except Exception as e:
        st.warning(f"Could not render chart: {e}")


if "history" not in st.session_state:
    st.session_state.history = []
if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None
if "space_id" not in st.session_state:
    st.session_state.space_id = PINNED_SPACE_ID

with st.sidebar:
    st.subheader("Genie space")
    if PINNED_SPACE_ID:
        st.code(PINNED_SPACE_ID, language=None)
        st.caption("Pinned via `GENIE_SPACE_ID`.")
    else:
        spaces = list_spaces()
        if spaces:
            labels = {f"{title} ({sid[:8]}...)": sid for sid, title in spaces}
            current = next((k for k, v in labels.items() if v == st.session_state.space_id), None)
            choice = st.selectbox(
                "Pick a space", list(labels.keys()),
                index=list(labels.keys()).index(current) if current else 0,
            )
            if labels[choice] != st.session_state.space_id:
                st.session_state.space_id = labels[choice]
                st.session_state.history = []
                st.session_state.conversation_id = None
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
        st.session_state.history = []
        st.session_state.conversation_id = None
        st.rerun()
    st.divider()
    st.subheader("About")
    st.markdown(
        "- Calls the **Genie Conversation API**\n"
        "- Shows the **generated SQL** (editable + rerun)\n"
        "- Renders a **chart** from the result\n"
        "- **Download** as CSV"
    )

SPACE_ID = st.session_state.space_id
if not SPACE_ID:
    st.info("Pick a Genie space in the sidebar to start.")
    st.stop()
st.caption(f"Genie space: `{SPACE_ID}`. Ask a question, see the SQL, the chart, and download the result.")

prompt = st.chat_input("Ask Genie about the data...")
if prompt:
    with st.spinner("Asking Genie..."):
        try:
            msg = ask_genie(SPACE_ID, prompt, st.session_state.conversation_id)
            st.session_state.conversation_id = msg.conversation_id
            if msg.status != MessageStatus.COMPLETED:
                status_name = getattr(msg.status, "value", str(msg.status))
                st.error(f"Genie returned status: **{status_name}**")
                st.markdown(_extract_error_details(msg))
                if msg.status == MessageStatus.FAILED:
                    st.info(
                        "**Common causes of FAILED:**\n\n"
                        "1. **Missing data grants.** The app's service principal needs "
                        "`USE SCHEMA` + `SELECT` on the catalog/schema this Genie space queries. "
                        "Run `GRANT SELECT ON SCHEMA <catalog>.<schema> TO \\`<sp-client-id>\\``.\n"
                        "2. **Question off-schema.** Genie cannot answer outside the tables curated in the space.\n"
                        "3. **SQL runtime error.** Genie's generated SQL hit a syntax or type error. "
                        "Check the SQL in the error block above; tail `<app-url>/logz` for the warehouse error."
                    )
            else:
                qa = next((a for a in (msg.attachments or []) if a.query), None)
                ta = next((a for a in (msg.attachments or []) if a.text), None)
                df = fetch_result(SPACE_ID, msg) if qa else None
                st.session_state.history.append({
                    "question": prompt,
                    "sql": format_sql(qa.query.query) if qa else None,
                    "description": qa.query.description if qa else None,
                    "text": ta.text.content if ta else None,
                    "df": df,
                })
        except Exception as e:
            st.error(f"Genie call failed: {e}")

for i, turn in enumerate(st.session_state.history):
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        if turn.get("text"):
            st.markdown(turn["text"])
        if turn.get("description"):
            st.caption(turn["description"])

        sql = turn.get("sql")
        if sql:
            st.markdown("**Generated SQL**")
            st.code(sql, language="sql")
            with st.expander("Edit and rerun", expanded=False):
                edited = st.text_area(
                    "SQL",
                    value=sql,
                    height=180,
                    key=f"sql_{i}",
                    label_visibility="collapsed",
                )
                if st.button("Rerun edited SQL", key=f"rerun_{i}"):
                    w = get_client()
                    try:
                        resp = w.statement_execution.execute_statement(
                            statement=edited, warehouse_id=get_warehouse_id(SPACE_ID), wait_timeout="30s"
                        )
                        if resp.status.state == StatementState.SUCCEEDED:
                            st.session_state.history[i]["df"] = result_to_df(resp)
                            st.session_state.history[i]["sql"] = format_sql(edited)
                            st.rerun()
                        else:
                            err = resp.status.error.message if resp.status.error else str(resp.status.state)
                            st.error(f"Statement failed: {err}")
                    except Exception as e:
                        st.error(f"Statement execution failed: {e}")

        df = turn.get("df")
        if isinstance(df, pd.DataFrame) and not df.empty:
            tab_table, tab_chart = st.tabs(["Table", "Chart"])
            with tab_table:
                st.dataframe(df, use_container_width=True)
                st.download_button(
                    "Download CSV",
                    data=df.to_csv(index=False).encode("utf-8"),
                    file_name=f"genie_result_{i+1}.csv",
                    mime="text/csv",
                    key=f"dl_{i}",
                )
            with tab_chart:
                render_chart(df, key=str(i))
        elif df is not None:
            st.info("Query returned no rows.")
