"""TopGenie: a Streamlit app on the Databricks Genie Conversation API."""
from __future__ import annotations

import os
import time

import pandas as pd
import plotly.express as px
import sqlparse
import streamlit as st
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.dashboards import MessageStatus
from databricks.sdk.service.sql import StatementState
from streamlit_ace import st_ace

PALETTE = px.colors.sequential.Plasma_r
TERMINAL = {MessageStatus.COMPLETED, MessageStatus.FAILED,
            MessageStatus.CANCELLED, MessageStatus.QUERY_RESULT_EXPIRED}
INT_TYPES = {"INT", "INTEGER", "LONG", "BIGINT", "SHORT", "SMALLINT", "BYTE", "TINYINT"}
FLOAT_TYPES = {"DOUBLE", "FLOAT", "DECIMAL", "NUMERIC"}
FAILED_HINTS = (
    "**Common causes of FAILED:**\n\n"
    "1. **Missing data grants.** The SP needs `USE SCHEMA` + `SELECT` on the schema this space queries.\n"
    "2. **Question off-schema.** Genie cannot answer outside the curated tables.\n"
    "3. **SQL runtime error.** See the SQL above; tail `<app-url>/logz` for the warehouse error."
)

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
    try:
        return [(s.space_id, s.title or s.space_id)
                for s in (get_client().genie.list_spaces().spaces or [])]
    except Exception:
        return []


def get_warehouse_id(space_id: str) -> str:
    return WAREHOUSE_ID or get_client().genie.get_space(space_id=space_id).warehouse_id


def format_sql(sql: str) -> str:
    try:
        return sqlparse.format(sql or "", reindent=True, keyword_case="upper") if sql else ""
    except Exception:
        return sql


def result_to_df(sr) -> pd.DataFrame:
    if not sr.manifest or not sr.manifest.schema:
        return pd.DataFrame()
    cols = sr.manifest.schema.columns
    rows = (sr.result.data_array if sr.result else None) or []
    df = pd.DataFrame(rows, columns=[c.name for c in cols])
    for c in cols:
        t = (c.type_name.value if hasattr(c.type_name, "value") else str(c.type_name)).upper()
        try:
            if t in INT_TYPES or t in FLOAT_TYPES:
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


def ask_genie(space_id: str, question: str, conversation_id: str | None):
    w = get_client()
    if conversation_id:
        p = w.genie.create_message(space_id=space_id, conversation_id=conversation_id, content=question)
        cid, mid = conversation_id, (p.id or p.message_id)
    else:
        p = w.genie.start_conversation(space_id=space_id, content=question)
        cid, mid = p.conversation_id, (p.message_id or (p.message.id if p.message else None))
    deadline = time.time() + 180
    msg = None
    while time.time() < deadline:
        msg = w.genie.get_message(space_id=space_id, conversation_id=cid, message_id=mid)
        if msg.status in TERMINAL:
            break
        time.sleep(1.5)
    return msg


def error_details(msg) -> str:
    parts = []
    if err := getattr(msg, "error", None):
        parts.append(getattr(err, "message", None) or str(err))
    for a in (msg.attachments or []):
        if a.text and a.text.content:
            parts.append(a.text.content)
        if a.query and a.query.query:
            parts.append(f"SQL Genie tried:\n```sql\n{a.query.query}\n```")
    return "\n\n".join(parts) or "(no additional details from the API)"


def fetch_result(space_id: str, msg) -> pd.DataFrame | None:
    qa = next((a for a in (msg.attachments or []) if a.query), None)
    if not qa:
        return None
    sr = get_client().genie.get_message_attachment_query_result(
        space_id=space_id, conversation_id=msg.conversation_id,
        message_id=msg.message_id or msg.id, attachment_id=qa.attachment_id,
    ).statement_response
    if sr.status.state != StatementState.SUCCEEDED:
        st.error(f"Query did not succeed: {sr.status.state}")
        return None
    return result_to_df(sr)


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
    y = c3.selectbox("Y axis", yopts,
                     index=yopts.index(num[0]) if num and num[0] in yopts else 0, key=f"y_{key}")

    plot = df[[x, y]].dropna()
    try:
        if chart == "Bar":
            plot = plot.sort_values(y, ascending=False)
            fig = px.bar(plot, x=x, y=y, color=y, color_continuous_scale=PALETTE,
                         hover_data={x: True, y: ":,.2f"})
            fig.update_layout(coloraxis_showscale=False)
        elif chart == "Line":
            fig = px.line(plot, x=x, y=y, markers=True)
            fig.update_traces(line=dict(width=2.5, color="#9C27B0"), marker=dict(size=8))
        elif chart == "Area":
            fig = px.area(plot, x=x, y=y)
            fig.update_traces(line=dict(width=2.5, color="#9C27B0"),
                              fillcolor="rgba(156, 39, 176, 0.25)")
        else:
            fig = px.scatter(plot, x=x, y=y, color=y, color_continuous_scale=PALETTE)
            fig.update_traces(marker=dict(size=10, line=dict(width=1, color="white")))
            fig.update_layout(coloraxis_showscale=False)

        fig.update_layout(
            plot_bgcolor="#fafafa", paper_bgcolor="#fafafa",
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

# Ask
prompt = st.chat_input("Ask Genie about the data...")
if prompt:
    with st.spinner("Asking Genie..."):
        try:
            msg = ask_genie(SPACE_ID, prompt, st.session_state.conversation_id)
            st.session_state.conversation_id = msg.conversation_id
            if msg.status != MessageStatus.COMPLETED:
                status = getattr(msg.status, "value", str(msg.status))
                st.error(f"Genie returned status: **{status}**")
                st.markdown(error_details(msg))
                if msg.status == MessageStatus.FAILED:
                    st.info(FAILED_HINTS)
            else:
                qa = next((a for a in (msg.attachments or []) if a.query), None)
                ta = next((a for a in (msg.attachments or []) if a.text), None)
                st.session_state.history.append({
                    "question": prompt,
                    "sql": format_sql(qa.query.query) if qa else None,
                    "description": qa.query.description if qa else None,
                    "text": ta.text.content if ta else None,
                    "df": fetch_result(SPACE_ID, msg) if qa else None,
                })
        except Exception as e:
            st.error(f"Genie call failed: {e}")

# History
for i, turn in enumerate(st.session_state.history):
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        if turn.get("text"):
            st.markdown(turn["text"])
        if turn.get("description"):
            st.caption(turn["description"])

        if turn.get("sql"):
            with st.expander("Edit and rerun SQL", expanded=False):
                edited = st_ace(value=turn["sql"], language="sql", theme="github",
                                keybinding="vscode", font_size=13, tab_size=2, wrap=True,
                                show_print_margin=False, auto_update=True, min_lines=8,
                                key=f"sql_{i}")
                if st.button("Rerun edited SQL", key=f"rerun_{i}"):
                    try:
                        resp = get_client().statement_execution.execute_statement(
                            statement=edited, warehouse_id=get_warehouse_id(SPACE_ID),
                            wait_timeout="30s",
                        )
                        if resp.status.state == StatementState.SUCCEEDED:
                            st.session_state.history[i]["df"] = result_to_df(resp)
                            st.session_state.history[i]["sql"] = format_sql(edited)
                            st.rerun()
                        else:
                            err = resp.status.error.message if resp.status.error else resp.status.state
                            st.error(f"Statement failed: {err}")
                    except Exception as e:
                        st.error(f"Statement execution failed: {e}")

        df = turn.get("df")
        if isinstance(df, pd.DataFrame) and not df.empty:
            tab_table, tab_chart = st.tabs(["Table", "Chart"])
            with tab_table:
                st.dataframe(df, use_container_width=True)
                st.download_button("Download CSV", data=df.to_csv(index=False).encode("utf-8"),
                                   file_name=f"genie_result_{i+1}.csv", mime="text/csv",
                                   key=f"dl_{i}")
            with tab_chart:
                render_chart(df, key=str(i))
        elif df is not None:
            st.info("Query returned no rows.")
