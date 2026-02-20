# code-gen-memory

An MCP (Model Context Protocol) server that gives AI coding agents **persistent memory across sessions** using a Neo4j graph database. Every code generation task is stored, searchable, and resumable — even weeks later.

---

## What It Does

Without this server, an AI agent forgets everything between sessions. With it:

- Every project and its progress is stored in Neo4j
- The agent can find past projects using **hybrid semantic + keyword search**
- Sessions can be resumed from exactly where they left off
- Parts of old projects (frontend, backend, DB schema) can be reused in new ones
- A full version history is maintained across every update

---

## How It Works

### The Core Loop

Every time the agent works on something, it:

1. Asks the user if this is a new project or a continuation
2. Searches memory if continuing (or reusing a part)
3. Generates code / makes changes
4. Saves a **delta summary** of what was done and what remains
5. Chains that summary to the previous one in the graph

### Delta Summaries

Each summary only captures what happened in *that session* — what was implemented, what files changed, and what's still pending. Previous summaries don't need to be repeated because they're all linked via `PREVIOUS_VERSION` in the graph. The full history is always traversable.

### History Chaining in Neo4j

```
Session 1:  (Project) → (Summary1)
Session 2:  (Project) → (Summary2) → (Summary1)
Session 3:  (Project) → (Summary3) → (Summary2) → (Summary1)
```

`HAS_LATEST_SUMMARY` always points to the most recent node. `HAS_SUMMARY` links to all of them.

---

## Search Architecture

Finding past projects uses a three-stage pipeline:

**Stage 1 — Semantic search** using `all-mpnet-base-v2` vector embeddings (cosine similarity via Neo4j vector index)

**Stage 2 — BM25 keyword search** using Neo4j full-text index

**Stage 3 — RRF fusion** combines both ranked lists into a single candidate set, then a **cross-encoder** (`ms-marco-MiniLM-L-6-v2`) reranks them for final precision

This means the search works well whether you describe a project conceptually ("the booking thing with room availability") or with specific keywords ("RoomBooking table foreign key").

---

## Prerequisites

- Python 3.9+
- Neo4j 5.x (running locally on `bolt://localhost:7687`)
- The following Python packages:

```bash
pip install mcp sentence-transformers neo4j
```

---

## Neo4j Setup

Run these once in Neo4j Browser or Cypher Shell to create the required indexes:

```cypher
CREATE FULLTEXT INDEX project_summary_fulltext_index
FOR (n:Summary) ON EACH [n.text];

CREATE VECTOR INDEX project_embedding_index
FOR (n:Summary) ON (n.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 768,
  `vector.similarity_function`: 'cosine'
}};
```

---

## Running the Server

```bash
python server.py
```

The server runs over stdio, suitable for direct integration with AI agent frameworks that support MCP.

---

## MCP Tools

| Tool | Description |
|------|-------------|
| `upsert_project_tool(name, question, summary, project_id?)` | Create a new project or update an existing one. Omit `project_id` for new projects. Always include it for updates to trigger history chaining. |
| `get_project_node(summary_id)` | Given a Summary node ID (e.g. from search results), returns the parent Project's `id`, `name`, and `question`. Use this to resolve project context after a search. |
| `get_latest_summary_tool(project_id)` | Returns the most recent summary for a project. Use this to load context before resuming work. |
| `hybrid_rerank_search(query_text, top_k, rrf_k)` | Finds relevant projects using BM25 + semantic RRF + cross-encoder reranking. Returns Summary nodes — follow up with `get_project_node` to get Project data. |
| `get_data_tool(node_ids)` | Fetches project info for a list of Summary node IDs. |
| `delete_project_tool(project_id)` | Permanently deletes a project and all its summaries. Use only when explicitly asked. |

---

## Graph Schema

```cypher
(Project {id, name, question, updated_at})
(Summary {id, text, embedding, created_at})

(Project)-[:HAS_SUMMARY]->(Summary)          // all summaries ever saved
(Project)-[:HAS_LATEST_SUMMARY]->(Summary)   // pointer to the most recent
(Summary)-[:PREVIOUS_VERSION]->(Summary)     // history chain: new → old
```

---

## Typical Usage Flow

### New project

```python
# 1. Generate code
# 2. Save with a readable name
upsert_project_tool(
    name="Booking System",
    question="Build a room booking system with availability checking",
    summary="Initial scaffold created. Routes defined, DB schema pending."
)
# → "Saved. Project: Booking System (abc-123), Summary: sum-001"
```

### Resume in a later session

```python
# 1. Search for the project
results = hybrid_rerank_search("booking system availability", top_k=5)

# 2. Resolve the project node from the top summary result
project = get_project_node(summary_id=results[0]['node_id'])
# → {project_id: "abc-123", project_name: "Booking System", ...}

# 3. Load the latest context
latest = get_latest_summary_tool(project_id="abc-123")

# 4. Continue coding, then save delta
upsert_project_tool(
    project_id="abc-123",
    name="Booking System",        # keep unchanged
    question="Build a room booking ...",  # keep unchanged
    summary="DB schema implemented. Auth endpoints still pending."
)
```

### New project reusing parts from an old one

```python
# Search for the reference project
results = hybrid_rerank_search("database schema booking system")
project = get_project_node(results[0]['node_id'])
ref = get_latest_summary_tool(project['project_id'])

# Build new project using extracted context
upsert_project_tool(
    name="Hotel Management",
    question="Build a hotel management system",
    summary="DB schema reused from Booking System (abc-123). Hotel-specific models pending."
    # no project_id — brand new project
)
```

---

## File Output Structure

Each task also writes to the local filesystem:

```
generated-code/
  booking-system/
    0001-initial-plan.md
    0002-db-schema.md
    generated/
      models.py
      routes.py
    MODIFIED_FILES.md
```

`MODIFIED_FILES.md` is updated after every task with a log of what was created or changed.

---

## Key Design Decisions

**Why delta summaries instead of full summaries?** Because the nodes are linked. The full history is always accessible by traversing `PREVIOUS_VERSION`. Repeating previous context in every summary wastes storage and embedding quality.

**Why cross-encoder reranking?** Vector search and BM25 both have blind spots. The cross-encoder reads the query and document together, giving much more accurate relevance scoring for the final ranking.

**Why `get_project_node` as a separate step?** `hybrid_rerank_search` returns Summary nodes (because that's what has embeddings). The Project node — which holds `name`, `question`, and `project_id` — is a separate node. Keeping the lookup explicit prevents the agent from using stale or assumed project metadata.