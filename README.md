# EHCD Hypernym Chatbot POC Microservice

## Overview
This service is a Flask-based backend microservice that provides an internal RAG (Retrieval-Augmented Generation) chatbot for EHCD data. It combines project data from PostgreSQL, education tabular data (Excel), and policy PDFs into searchable FAISS indexes, then uses Azure OpenAI for embeddings and chat responses. The frontend consumes the backend API directly.

## Tech Stack
- Python 3.12
- Flask (web server + routes)
- Azure OpenAI (chat + embeddings)
- LangChain (document wrappers, splitters)
- FAISS (vector search)
- PostgreSQL (project and user data)
- Redis (chat history, reindex queue, cache)
- SQLite (internal metadata for documents/sessions)
- Azure Blob Storage (optional sync for Excel and policy PDFs)
- pandas + openpyxl (Excel parsing)
- PyPDF (PDF parsing)

## High-Level Architecture (Frontend to Backend)
1. Frontend sends a chat request to the backend API.
2. The backend checks the user’s RBAC permissions from PostgreSQL (production EHDC database).
3. A user-specific FAISS index is selected (admin vs manager audience).
4. The user query is embedded once, then searched across:
   - Project index (PostgreSQL data)
   - Education tabular index (Excel)
   - Policy index (PDFs)
5. Retrieved context is sent to Azure OpenAI for a streamed response.
6. Response is streamed back to the frontend as HTML (final replacement payload included).

## Data Sources and Indexes
- **Projects (PostgreSQL)**
  - Audience-specific FAISS index per user.
  - Admin index includes all projects; manager index includes managed projects and notes as permitted.
  - Index rebuild is triggered via Redis queue on changes or permission shifts.

- **Education Tabular (Excel)**
  - Source from local `data/docs/tabular` or Azure Blob Storage.
  - Indexed into FAISS with normalized rows and sheet intros.
  - Rebuilt periodically in a background thread.

- **Policy PDFs (Azure Blob Storage)**
  - Synced from blob prefix `policies/` into local cache.
  - Chunked and indexed into FAISS.
  - Rebuilt periodically in a background thread.

## API (Frontend to Backend Contract)
### `POST /api/query`
**Purpose:** Chat query endpoint (streaming response).

**Request JSON**
- `query` (string, required)  
  The user question or instruction.
- `user_id` (int, required)  
  The EHDC user ID used for RBAC and indexing.
- `conversation_id` (string, optional; default `default`)  
  Conversation scope for chat history in Redis.

**Headers**
- `Content-Type: application/json`

**Response**
- `text/html` streaming response
- Streams plain text chunks as they are generated.
- Finishes with a `<replace>...</replace>` tag containing the full HTML response from the model.

**Errors**
- `400` if `query` is empty
- `400` if `user_id` missing
- `500` on processing errors

## API Working Flow (Request Lifecycle)
1. Validate `query` and `user_id`.
2. Load conversation history from Redis (`uid:<user_id>:conv:<conversation_id>`).
3. Fetch user profile and permissions from PostgreSQL.
4. Ensure user-specific FAISS index exists (enqueue rebuild if stale).
5. Embed the query once and search indexes in order: projects (user-specific), education tabular (if permitted), policy (global).
6. Concatenate retrieved context and call Azure OpenAI chat completions (streaming).
7. Stream response chunks to the client and persist full response back to Redis.

## RBAC and User Architecture
RBAC is enforced through PostgreSQL tables (role and feature mappings). Each user gets an audience-specific index:
- **Admin audience**: `admin_for_<user_id>`
- **Manager audience**: `manager_<user_id>`

Access features checked:
- `ALL_PROJECTS`
- `BUDGET_INFO`
- `USER_MANAGEMENT`
- `NOTES`
- `EDUCATION_DASH`

These features determine:
- Which projects are indexed
- Whether budget info is included
- Whether user directory data is included
- Whether personal notes are included

## Background Jobs
- **Education index rebuild** every 2 hours (or on change)
- **Per-user index rebuild** via Redis queue `reindex_jobs`
- **Policy index rebuild** every hour

## Local Storage
- `./data/docs` for local documents
- `./data/hashes` for index fingerprints
- `./data/faiss` for FAISS indexes
- `documents.db` for document listing
- `sessions.db` for login sessions

## Configuration (Environment Variables)
### Database
- `PG_HOST` (default `127.0.0.1`)
- `PG_DB` (default `postgres`)
- `PG_USER` (default `postgres`)
- `PG_PASS` (default `wgRV|X&77:8#`)
- `PG_PORT` (default `5432`)
Note: In production, this service connects to the EHDC production PostgreSQL instance.

### RBAC
- `RBAC_USER_ROLES_TABLE` (default `user_management_user_roles`)
- `DB_SCHEMA` (default `public`)

### Azure OpenAI
- `ENDPOINT_URL` (default `https://app-openai-uae.cognitiveservices.azure.com/`)
- `DEPLOYMENT_NAME` (default `gpt-4o`)
- `AZURE_OPENAI_API_KEY` (required)
- `AZURE_OPENAI_API_VERSION` (default `2025-01-01-preview`)
- `AZURE_EMBEDDING_DEPLOYMENT` (default `text-embedding-3-small`)

### Azure Blob Storage (Optional)
- `AZURE_BLOB_CONN_STR`
- `AZURE_BLOB_CONTAINER`
- `AZURE_BLOB_PREFIX` (tabular only; policy uses `policies/`)

### Redis
- `REDIS_HOST` (default `localhost`)
- `REDIS_PORT` (default `6379`)

### Storage
- `DATA_ROOT` (default `./data`)

## Deployment (Docker)
```
docker compose up --build
```

This will start:
- The Flask app on `http://localhost:8080`
- Redis on port `6380` (host) -> `6379` (container)

## Key Files
- `app.py` Main service
- `education.py` Excel indexing and search
- `policy.py` Policy PDF indexing and search
- `doc.py` Document metadata helpers
- `Dockerfile`, `docker-compose.yml` Container setup
- `requirements.txt` Dependencies
