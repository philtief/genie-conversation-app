"""TopGenie: a Streamlit app on the Databricks Genie Conversation API.

Asks a Genie space a question, shows the generated SQL (editable + rerun),
renders a chart from the result, and offers a CSV download.

Configure with two environment variables:
    GENIE_SPACE_ID         -- the Genie space to query
    DATABRICKS_WAREHOUSE_ID -- the warehouse used for "Rerun edited SQL"
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.dashboards import MessageStatus
from databricks.sdk.service.sql import StatementState

SPACE_ID = os.environ["GENIE_SPACE_ID"]
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID")

st.set_page_config(page_title="TopGenie", page_icon=":bar_chart:", layout="wide")
st.title("TopGenie")
st.caption(f"Genie space: `{SPACE_ID}`. Ask a question, see the SQL, the chart, and download the result.")


@st.cache_resource
def get_client() -> WorkspaceClient:
    if os.environ.get("DATABRICKS_APP_NAME"):
        return WorkspaceClient()
    return WorkspaceClient(profile=os.environ.get("DATABRICKS_PROFILE", "DEFAULT"))


@st.cache_resource
def get_warehouse_id() -> str:
    if WAREHOUSE_ID:
        return WAREHOUSE_ID
    return get_client().genie.get_space(space_id=SPACE_ID).warehouse_id


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
    coerced = [[_coerce(v, t) for v, t in zip(row, types)] for row in rows]
    return pd.DataFrame(coerced, columns=names)


def ask_genie(question: str, conversation_id: str | None):
    w = get_client()
    if conversation_id:
        return w.genie.create_message_and_wait(
            space_id=SPACE_ID, conversation_id=conversation_id, content=question
        )
    return w.genie.start_conversation_and_wait(space_id=SPACE_ID, content=question)


def fetch_result(msg) -> pd.DataFrame | None:
    qa = next((a for a in (msg.attachments or []) if a.query), None)
    if not qa:
        return None
    w = get_client()
    r = w.genie.get_message_attachment_query_result(
        space_id=SPACE_ID,
        conversation_id=msg.conversation_id,
        message_id=msg.message_id or msg.id,
        attachment_id=qa.attachment_id,
    )
    sr = r.statement_response
    if sr.status.state != StatementState.SUCCEEDED:
        st.error(f"Query did not succeed: {sr.status.state}")
        return None
    return result_to_df(sr)


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
        plot_df = df[[x, y]]
        if chart_type == "Bar":
            st.bar_chart(plot_df, x=x, y=y, use_container_width=True)
        elif chart_type == "Line":
            st.line_chart(plot_df, x=x, y=y, use_container_width=True)
        elif chart_type == "Area":
            st.area_chart(plot_df, x=x, y=y, use_container_width=True)
        elif chart_type == "Scatter":
            st.scatter_chart(plot_df, x=x, y=y, use_container_width=True)
    except Exception as e:
        st.warning(f"Could not render chart: {e}")


if "history" not in st.session_state:
    st.session_state.history = []
if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None

with st.sidebar:
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

prompt = st.chat_input("Ask Genie about the data...")
if prompt:
    with st.spinner("Asking Genie..."):
        try:
            msg = ask_genie(prompt, st.session_state.conversation_id)
            st.session_state.conversation_id = msg.conversation_id
            if msg.status != MessageStatus.COMPLETED:
                st.error(f"Genie returned status {msg.status}")
            else:
                qa = next((a for a in (msg.attachments or []) if a.query), None)
                ta = next((a for a in (msg.attachments or []) if a.text), None)
                df = fetch_result(msg) if qa else None
                st.session_state.history.append({
                    "question": prompt,
                    "sql": qa.query.query if qa else None,
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
            with st.expander("Generated SQL", expanded=True):
                edited = st.text_area(
                    "Edit and rerun",
                    value=sql,
                    height=180,
                    key=f"sql_{i}",
                    label_visibility="collapsed",
                )
                if st.button("Rerun edited SQL", key=f"rerun_{i}"):
                    w = get_client()
                    try:
                        resp = w.statement_execution.execute_statement(
                            statement=edited, warehouse_id=get_warehouse_id(), wait_timeout="30s"
                        )
                        if resp.status.state == StatementState.SUCCEEDED:
                            st.session_state.history[i]["df"] = result_to_df(resp)
                            st.session_state.history[i]["sql"] = edited
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
