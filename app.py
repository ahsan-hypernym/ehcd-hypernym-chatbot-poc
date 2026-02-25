"""
EHCD Hypernym Chatbot — Main Flask Application
Tool-based architecture with Azure OpenAI function calling.
"""

import os
import json
import re
import time
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

import psycopg2
from psycopg2 import InterfaceError, OperationalError
from psycopg2.pool import ThreadedConnectionPool

import redis
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    Response,
    stream_with_context,
    session,
    flash,
    redirect,
    url_for,
    send_file,
)
from functools import wraps

from openai import AzureOpenAI

# FAISS + embeddings (still needed for education & policy searches)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import AzureOpenAIEmbeddings

from doc import Documents
from education import (
    TabularConfig,
    update_tabular_index_if_changed,
)
from policy import update_policy_index_if_changed, PolicyConfig
from emb_pace import PacedEmbeddings

# New modular imports
from rbac import fetch_user_profile, get_user_access_flags
from tools import build_available_tools, generate_tool_response
from chart_engine import detect_chart_opportunity

# ────────────────────────────────────────────────────────────────────────────────
# CONFIG & LOGGING
# ────────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = "fs78sf7s8d6v7sdy7sdbds7v"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Redis (for chat history)
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    db=0,
)


@dataclass(frozen=True)
class CFG:
    # Postgres
    PG_HOST: str = os.getenv("PG_HOST", "127.0.0.1")
    PG_DB: str = os.getenv("PG_DB", "postgres")
    PG_USER: str = os.getenv("PG_USER", "postgres")
    PG_PASS: str = os.getenv("PG_PASS", "wgRV|X&77:8#")
    PG_PORT: int = int(os.getenv("PG_PORT", "5432"))

    # Azure OpenAI
    AZURE_OPENAI_ENDPOINT = os.getenv(
        "ENDPOINT_URL", "https://app-openai-uae.cognitiveservices.azure.com/"
    )
    AZURE_OPENAI_DEPLOYMENT = os.getenv("DEPLOYMENT_NAME", "gpt-4o")
    AZURE_OPENAI_KEY: str = os.getenv("AZURE_OPENAI_API_KEY", "")
    AZURE_OPENAI_API_VERSION: str = os.getenv(
        "AZURE_OPENAI_API_VERSION", "2025-01-01-preview"
    )
    AZURE_EMBED_DEPLOYMENT: str = os.getenv(
        "AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
    )

    # Local storage
    ROOT: str = os.getenv("DATA_ROOT", "./data")
    DOC_DIR: str = os.path.join(ROOT, "docs")
    HASH_DIR: str = os.path.join(ROOT, "hashes")
    FAISS_DIR: str = os.path.join(ROOT, "faiss")

    # Chunking (for education & policy)
    CHUNK_SIZE: int = 900
    CHUNK_OVERLAP: int = 120


cfg = CFG()

# ────────────────────────────────────────────────────────────────────────────────
# Education Tabular (Excel) Config
# ────────────────────────────────────────────────────────────────────────────────
EDU_CFG = TabularConfig(
    tabular_dir=os.path.join(cfg.DOC_DIR, "tabular"),
    faiss_dir=os.path.join(cfg.FAISS_DIR, "education_tabular"),
    hash_json=os.path.join(cfg.HASH_DIR, "education_tabular.sha.json"),
    blob_conn_str=os.getenv("AZURE_BLOB_CONN_STR", ""),
    blob_container=os.getenv("AZURE_BLOB_CONTAINER", ""),
    blob_prefix=os.getenv("AZURE_BLOB_PREFIX", ""),
    throttle_seconds=600,
)

POLICY_CFG = PolicyConfig(
    faiss_dir=os.path.join(cfg.FAISS_DIR, "policy"),
    hash_json=os.path.join(cfg.HASH_DIR, "policy.sha.json"),
    blob_conn_str=os.getenv("AZURE_BLOB_CONN_STR", ""),
    blob_container=os.getenv("AZURE_BLOB_CONTAINER", ""),
    blob_prefix="policies/",
    local_dir=os.path.join(cfg.DOC_DIR, "policies"),
    throttle_seconds=600,
)

os.makedirs(cfg.DOC_DIR, exist_ok=True)
os.makedirs(cfg.HASH_DIR, exist_ok=True)
os.makedirs(cfg.FAISS_DIR, exist_ok=True)

# ────────────────────────────────────────────────────────────────────────────────
# Azure OpenAI clients
# ────────────────────────────────────────────────────────────────────────────────
client = AzureOpenAI(
    azure_endpoint=cfg.AZURE_OPENAI_ENDPOINT,
    api_key=cfg.AZURE_OPENAI_KEY,
    api_version=cfg.AZURE_OPENAI_API_VERSION,
)

embeddings = AzureOpenAIEmbeddings(
    azure_deployment=cfg.AZURE_EMBED_DEPLOYMENT,
    openai_api_key=cfg.AZURE_OPENAI_KEY,
    azure_endpoint=cfg.AZURE_OPENAI_ENDPOINT,
    openai_api_version=cfg.AZURE_OPENAI_API_VERSION,
)
splitter = RecursiveCharacterTextSplitter(
    chunk_size=cfg.CHUNK_SIZE, chunk_overlap=cfg.CHUNK_OVERLAP
)
emb = PacedEmbeddings(embeddings, tpm_limit=200_000, batch_size=32)

# Document management (optional UI)
documents = Documents()
documents.save_local_files_to_db()

# ────────────────────────────────────────────────────────────────────────────────
# DATABASE CONNECTION POOL
# ────────────────────────────────────────────────────────────────────────────────
_pool = ThreadedConnectionPool(
    minconn=2,
    maxconn=10,
    host=cfg.PG_HOST,
    dbname=cfg.PG_DB,
    user=cfg.PG_USER,
    password=cfg.PG_PASS,
    port=cfg.PG_PORT,
    sslmode="require",
    connect_timeout=10,
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=5,
    application_name="ehcd-hypernym-chatbot",
)


@contextmanager
def pg_conn():
    """Get a connection from the pool with auto-commit/rollback."""
    conn = _pool.getconn()
    try:
        # Pre-ping to avoid reusing stale SSL connections from pool.
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except (OperationalError, InterfaceError):
            _pool.putconn(conn, close=True)
            conn = _pool.getconn()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

        yield conn
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except (OperationalError, InterfaceError):
            pass
        if isinstance(e, (OperationalError, InterfaceError)):
            _pool.putconn(conn, close=True)
            conn = None
        raise
    finally:
        if conn is not None:
            _pool.putconn(conn)


# ────────────────────────────────────────────────────────────────────────────────
# CHAT HISTORY (Redis)
# ────────────────────────────────────────────────────────────────────────────────
def get_conversation_history(user_key: str) -> list:
    h = redis_client.get(f"user_{user_key}_history")
    return json.loads(h) if h else []


def save_conversation_history(user_key: str, history: list):
    redis_client.set(f"user_{user_key}_history", json.dumps(history), ex=3600)


# ────────────────────────────────────────────────────────────────────────────────
# BACKGROUND INDEX BUILDERS (education & policy only)
# ────────────────────────────────────────────────────────────────────────────────
def background_education_rebuilder():
    while True:
        try:
            update_tabular_index_if_changed(EDU_CFG, emb)
        except Exception as e:
            logger.error(f"[EducationRebuilder] Failed: {e}")
        time.sleep(7200)  # every 2 hours


def background_policy_rebuilder():
    while True:
        try:
            update_policy_index_if_changed(POLICY_CFG, splitter, emb)
        except Exception as e:
            logger.error(f"[PolicyRebuilder] Failed: {e}")
        time.sleep(86400 * 3)  # every 3 days


# ────────────────────────────────────────────────────────────────────────────────
# MAIN API ENDPOINT (Tool-based)
# ────────────────────────────────────────────────────────────────────────────────
@app.route("/api/query", methods=["POST"])
def handle_query():
    payload = request.get_json(force=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Empty query"}), 400

    rbac_user_id_raw = payload.get("user_id")
    if rbac_user_id_raw is None:
        return jsonify({"error": "user_id is required"}), 400
    rbac_user_id = int(rbac_user_id_raw)

    conv_id = (payload.get("conversation_id") or "default").strip()
    history_key = f"uid:{rbac_user_id}:conv:{conv_id}"

    conversation_history = get_conversation_history(history_key)
    conversation_history.append({"role": "user", "content": query})

    # Fetch user info and available tools
    with pg_conn() as conn:
        user_profile = fetch_user_profile(conn, rbac_user_id)
        user_name = (
            user_profile.get("full_name_en")
            or user_profile.get("full_name_ar")
            or "Unknown User"
        )
        user_role = user_profile.get("designation") or ""
        user_email = user_profile.get("email") or ""
        user_contact_no = user_profile.get("contact_no") or ""

        available_tools = build_available_tools(conn, rbac_user_id)

    def generate():
        assistant_response = ""
        tool_results_for_chart = []

        try:
            for chunk in generate_tool_response(
                query=query,
                conversation_history=conversation_history,
                available_tools=available_tools,
                user_id=rbac_user_id,
                user_name=user_name,
                user_role=user_role,
                user_email=user_email,
                user_contact_no=user_contact_no,
                client=client,
                model=cfg.AZURE_OPENAI_DEPLOYMENT,
                pg_conn_fn=pg_conn,
                edu_cfg=EDU_CFG,
                policy_cfg=POLICY_CFG,
                emb=emb,
                tool_results_collector=tool_results_for_chart,
            ):
                assistant_response += chunk
                # Stream plain text (strip HTML for progressive display)
                plain_text_chunk = re.sub(r"<[^>]*>", "", chunk)
                yield plain_text_chunk

        except Exception as e:
            logger.error(f"Error in tool response generation: {e}")
            error_msg = (
                "I encountered an error processing your request. "
                "Please try again or rephrase your question."
            )
            assistant_response = error_msg
            yield error_msg

        # Save conversation history
        conversation_history.append(
            {"role": "assistant", "content": assistant_response}
        )
        save_conversation_history(history_key, conversation_history)

        # Detect chart opportunity and build final payload
        chart_data = None
        try:
            chart_data = detect_chart_opportunity(
                query, tool_results_for_chart, assistant_response
            )
        except Exception as e:
            logger.error(f"Chart detection error: {e}")

        # Send the final <replace> payload
        if assistant_response:
            if chart_data:
                final_payload = json.dumps(
                    {"html": assistant_response, "chart_data": chart_data},
                    ensure_ascii=False,
                    default=str,
                )
                yield f"<replace>{final_payload}</replace>"
            else:
                yield f"<replace>{assistant_response}</replace>"

    return Response(
        stream_with_context(generate()),
        content_type="text/html",
        headers={"Content-Encoding": "chunked"},
    )


# ────────────────────────────────────────────────────────────────────────────────
# LOGIN / SESSIONS
# ────────────────────────────────────────────────────────────────────────────────
credentials = {"hypernym1": "hyper@chatbot", "hypernym2": "hyper@chatbot"}
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=1)


def init_db():
    conn = sqlite3.connect("sessions.db")
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS active_sessions
           (username TEXT PRIMARY KEY, last_active TIMESTAMP)"""
    )
    conn.commit()
    conn.close()


def add_session(username):
    conn = sqlite3.connect("sessions.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO active_sessions (username, last_active) VALUES (?, ?)",
        (username, datetime.now()),
    )
    conn.commit()
    conn.close()


def remove_session(username):
    conn = sqlite3.connect("sessions.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM active_sessions WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def count_active_sessions():
    cleanup_expired_sessions()
    conn = sqlite3.connect("sessions.db")
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM active_sessions")
    c = cur.fetchone()[0]
    conn.close()
    return c


def is_user_logged_in(username):
    cleanup_expired_sessions()
    conn = sqlite3.connect("sessions.db")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM active_sessions WHERE username = ?", (username,))
    r = cur.fetchone()
    conn.close()
    return r is not None


def cleanup_expired_sessions():
    expiration_time = datetime.now() - app.config["PERMANENT_SESSION_LIFETIME"]
    conn = sqlite3.connect("sessions.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM active_sessions WHERE last_active < ?", (expiration_time,))
    conn.commit()
    conn.close()


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session or not is_user_logged_in(session["username"]):
            flash("Please log in to access this page.")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated_function


@app.route("/", methods=["GET", "POST"])
def login():
    init_db()
    if count_active_sessions() >= 2:
        flash(
            "Maximum number of users are currently logged in. "
            "Please wait until someone logs out."
        )
        return render_template("login.html")
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if credentials.get(username) == password:
            if is_user_logged_in(username):
                flash("This user is already logged in from another session.")
                return render_template("login.html")
            session["username"] = username
            session.permanent = True
            add_session(username)
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials. Please try again.")
            return render_template("login.html")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    username = session.pop("username", None)
    if username:
        remove_session(username)
    flash("You have been logged out.")
    return redirect(url_for("login"))


@app.route("/home")
@login_required
def index():
    return render_template("index.html")


# ────────────────────────────────────────────────────────────────────────────────
# DOCS ROUTES
# ────────────────────────────────────────────────────────────────────────────────
@app.route("/documents")
@login_required
def list_documents():
    if session.get("username") == "hypernym1":
        document_list = documents.fetch_documents()
        return render_template("documents.html", documents=document_list)
    else:
        return "Unauthorized", 401


@app.route("/download/<int:document_id>")
def download_document(document_id):
    document = documents.get_document_path(document_id)
    if document:
        document_name, file_path = document
        return send_file(file_path, as_attachment=True)
    return "Document not found", 404


# ────────────────────────────────────────────────────────────────────────────────
# START BACKGROUND THREADS
# ────────────────────────────────────────────────────────────────────────────────
threading.Thread(target=background_education_rebuilder, daemon=True).start()
threading.Thread(target=background_policy_rebuilder, daemon=True).start()

# ────────────────────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Starting EHCD Chatbot (tool-based architecture)")
    app.run(host="0.0.0.0", port=8080)
