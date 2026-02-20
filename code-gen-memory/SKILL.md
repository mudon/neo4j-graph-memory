---
name: code-gen-memory
description: Code generation with persistent memory using Neo4j graph database. Always prompts user for new-or-existing before any action. Semantic search only runs for continuations or targeted part-reuse. Maintains full version history chaining.
allowed-tools:
  - bash_tool
  - str_replace
  - view
  - create_file
---

# code-gen-memory

Use this Skill when:
- User wants to generate code with persistent memory across sessions
- Need to track code generation tasks and prevent duplicate projects
- Want semantic search across past code generation work
- Building related code features over multiple sessions

## Mental Model

- **Prompt-First**: ALWAYS ask the user whether this is a new project or continuing an existing one before doing anything
- **Conditional Search**: Semantic search is only triggered in two cases: (1) user says it's a continuation, or (2) user says it's a new project but wants to reuse a part (frontend / backend / database) from a previous project
- **History Chaining**: Each update creates a new Summary node chained to the previous one via `PREVIOUS_VERSION`
- **Latest Pointer**: `HAS_LATEST_SUMMARY` always points to the most recent summary
- **Delta Summaries**: Each summary captures only what was done in that session/task plus what remains — no need to repeat previous summaries since nodes are linked via `PREVIOUS_VERSION`

---

## Core Architecture

### MCP Tools Available

| Tool | Purpose |
|------|---------|
| `upsert_project_tool(name, question, summary, project_id?)` | Create or update a project. Requires a human-readable `name`. Auto-chains new summary to previous via `PREVIOUS_VERSION`. |
| `get_project_node(summary_id)` | Fetch the Project node for a given Summary node ID. Use to resolve `project_id` and `name` when only a summary ID is known. |
| `get_latest_summary_tool(project_id)` | Fetch the single most recent summary for a project. Use to resume work. |
| `hybrid_rerank_search(query_text, top_k, rrf_k)` | Hybrid BM25 + semantic search with cross-encoder reranking. Best for finding relevant projects. |
| `get_data_tool(node_ids)` | Fetch project info and summaries for a list of Summary node IDs. |
| `delete_project_tool(project_id)` | Delete a project and all its nodes. Use ONLY when explicitly asked. |

### Graph Schema

```cypher
(Project {id, name, question, updated_at})
(Summary {id, text, embedding, created_at})

(Project)-[:HAS_SUMMARY]->(Summary)         // All summaries ever
(Project)-[:HAS_LATEST_SUMMARY]->(Summary)  // Only the most recent
(Summary)-[:PREVIOUS_VERSION]->(Summary)    // History chain: new → old

CREATE VECTOR INDEX project_embedding_index IF NOT EXISTS
FOR (s:Summary) ON s.embedding
OPTIONS {indexConfig: {
  `vector.dimensions`: 768,
  `vector.similarity_function`: 'cosine'
}}
```

### How History Chaining Works

Each call to `upsert_project_tool` with an existing `project_id`:
1. Creates a **new** Summary node
2. Moves `HAS_LATEST_SUMMARY` to the new summary
3. Links new → old via `PREVIOUS_VERSION`
4. Keeps `HAS_SUMMARY` on all summaries for full history

```
Query 1 (plan):
  (Project)-[:HAS_LATEST_SUMMARY]->(Summary1)
  (Project)-[:HAS_SUMMARY]->(Summary1)

Query 2 (execute):
  (Project)-[:HAS_LATEST_SUMMARY]->(Summary2)
  (Project)-[:HAS_SUMMARY]->(Summary1, Summary2)
  (Summary2)-[:PREVIOUS_VERSION]->(Summary1)

Session 2 (continue coding):
  (Project)-[:HAS_LATEST_SUMMARY]->(Summary3)
  (Project)-[:HAS_SUMMARY]->(Summary1, Summary2, Summary3)
  (Summary3)-[:PREVIOUS_VERSION]->(Summary2)-[:PREVIOUS_VERSION]->(Summary1)
```

### Project Structure

```
Working Directory/
  generated-code/
    project-name/
      0001-initial-plan.md
      0002-execution.md
      0003-continued-coding.md
      generated/
        component.tsx
        utils.ts
      MODIFIED_FILES.md
```

---

## STEP 1 — ALWAYS PROMPT THE USER FIRST

**Before doing anything — no search, no generation — you MUST ask the user:**

```
Before I start, I need to know:

1. Is this a **new project** or are you **continuing an existing project**?
2. If it's a **new project**: do you want to reuse any part (frontend, backend,
   or database design) from a previous project?

Please let me know so I can set things up correctly.
```

Wait for the user's answer before proceeding. Do not skip this step even if the user's message seems obvious. The only exception is if the user has already clearly answered both questions in their initial message (e.g. "new project, no reuse" or "continue the booking system").

---

## STEP 2 — ROUTE BASED ON USER ANSWER

### Route A: "This is a continuation of an existing project"

→ **Trigger hybrid rerank search immediately.**

```python
search_results = hybrid_rerank_search(
    query_text=user_question,
    top_k=9,
    rrf_k=60
)
```

If a match is found, resolve the project node. `hybrid_rerank_search` returns Summary-level data — use `get_project_node` to get the authoritative `project_id` and `name`:

```python
top_summary_id = search_results[0]['node_id']  # summary node id from search result

project_node = get_project_node(summary_id=top_summary_id)
project_id = project_node['project_id']
project_name = project_node['project_name']
project_question = project_node['question']

latest = get_latest_summary_tool(project_id=project_id)
```

Inform the user:
```
## Resuming Project

**Project:** {project_name} ({project_id})
**Original Question:** {project_question}

**Latest Summary:**
{latest['latest_summary']}

Continuing from here...
```

Generate code/content building on the latest summary. After finishing, save a **delta summary** — pass the existing `name` so the project node is not accidentally renamed:

```python
result = upsert_project_tool(
    project_id=project_id,        # Existing ID — triggers chaining
    name=project_name,            # Keep existing name unchanged
    question=project_question,    # Keep original question unchanged
    summary=delta_summary
)
# New Summary node created and chained: new → old
```

If no match found, do NOT auto-create a new project. Ask for clarification:
```
I couldn't find a matching project in memory.
Could you clarify the project name or description so I can locate it?
```

---

### Route B: "This is a new project, no reuse from previous"

→ **Do NOT run any search.** Proceed directly to code generation.

Inform the user:
```
## Creating New Project

Starting fresh — no existing project will be reused.

**Request:** {question}
```

Generate the code/content from scratch, create an initial summary capturing the current state and what's pending, then save. Derive a short human-readable `name` from the user's request (e.g. `"Booking System"`, `"Hotel Management"`):

```python
result = upsert_project_tool(
    name="Booking System",         # Short, human-readable project name
    question=question,
    summary=initial_summary
    # project_id omitted — auto-generated
)
# Returns: "Saved. Project: Booking System ({new_project_id}), Summary: {summary_id}"
```

Parse and store the returned `project_id` for this session.

---

### Route C: "This is a new project, but I want to reuse [frontend / backend / database] from a previous project"

→ **Trigger a targeted hybrid rerank search** scoped to the part the user wants to reuse.

If the user hasn't specified which part, ask:
```
Which part from a previous project would you like to reuse?
- Frontend (UI components, design system)
- Backend (API structure, services, logic)
- Database (schema, entities, relationships)
- Other (please describe)
```

Then search using that part as the query context:

```python
# Example: user wants to reuse database design from booking system
search_results = hybrid_rerank_search(
    query_text=f"{reuse_part} design: {reuse_description}",
    top_k=9,
    rrf_k=60
)
```

If a match is found, resolve the project node:

```python
top_summary_id = search_results[0]['node_id']

project_node = get_project_node(summary_id=top_summary_id)
project_id_reference = project_node['project_id']
project_name_reference = project_node['project_name']
project_question = project_node['question']

# Load its latest summary to extract the relevant part
reference_latest = get_latest_summary_tool(project_id=project_id_reference)
```

Inform the user:
```
## Found Reference Project

**Project:** {project_name_reference} ({project_id_reference})
**Original Question:** {project_question}

I'll extract the {reuse_part} design from this project and apply it to your new one.
```

Generate the new project incorporating the referenced part. Create a **brand new project** — derive a fresh `name` for it, do NOT continue the old one:

```python
result = upsert_project_tool(
    name="Hotel Management",       # New project's own name
    question=new_question,
    summary=new_summary_with_reference_context
    # No project_id — this is a brand new project
)
```

The summary should document what was borrowed and from which reference project ID.

If no match found, ask for clarification or the project ID directly.

---

## STEP 3 — WITHIN THE SAME SESSION (Follow-up Queries)

Once a `project_id` is established in the current session, **do not re-prompt** on follow-up queries. The project context is already known.

For follow-up queries in the same session:
- Use the already-known `project_id`
- Call `get_latest_summary_tool(project_id)` to refresh context if needed
- Generate new code/content
- Save a delta summary with `upsert_project_tool(project_id=..., ...)` to chain a new node

---

## STEP 4 — SAVE GENERATED FILES

```bash
project_name=$(echo "$question" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//' | sed 's/-$//')

mkdir -p "generated-code/$project_name/generated"

task_num=$(find "generated-code/$project_name" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l)
task_num=$((task_num + 1))
task_id=$(printf "%04d" $task_num)
```

Save task documentation:

```bash
cat > "generated-code/$project_name/${task_id}-implementation.md" <<EOF
# Task: $question
**Date:** $(date +%Y-%m-%d)
**Project ID:** $project_id
**Task Number:** $task_id

## Request
$question

## Generated/Modified Files
[list files here]

## Implementation Notes
[key points]

## Code Changes
[code excerpts]
EOF
```

Update MODIFIED_FILES.md:

```bash
{
    echo "# Modified/Created Files Tracker"
    echo ""
    echo "## Task ${task_id} - $(date +%Y-%m-%d)"
    echo "**Request:** $question"
    echo ""
    echo "### Files Created"
    echo ""
    echo "### Files Modified"
    echo ""
    echo "---"
    echo ""
    if [ -f "generated-code/$project_name/MODIFIED_FILES.md" ]; then
        tail -n +2 "generated-code/$project_name/MODIFIED_FILES.md"
    fi
} > "generated-code/$project_name/MODIFIED_FILES.md.new"

mv "generated-code/$project_name/MODIFIED_FILES.md.new" "generated-code/$project_name/MODIFIED_FILES.md"
```

---

## Multi-Session Example: Booking System

### Session 1 — Query 1: "Create a plan for my booking system."

```
AI → Prompts: "New project or continuing existing? Any reuse?"
User → "New project, no reuse"

Route B: No search.

upsert_project_tool(
    name="Booking System",
    question="Create a plan for my booking system",
    summary="[plan summary: phases 1-3 defined, none implemented yet]"
)
→ "Saved. Project: Booking System (abc-123), Summary: sum-001"

Graph:
  (Project:abc-123 {name:"Booking System"})-[:HAS_LATEST_SUMMARY]->(Summary:sum-001)
  (Project:abc-123)-[:HAS_SUMMARY]->(Summary:sum-001)
```

### Session 1 — Query 2: "Execute and create the plan that has been planned."

```
(Same session — project_id abc-123 already known. No re-prompt.)

get_latest_summary_tool("abc-123") → sum-001 text
Generate execution code.

upsert_project_tool(
    project_id="abc-123",
    name="Booking System",            # Keep existing name
    question="Create a plan for my booking system",
    summary="[delta: phase 1 implemented. phase 2 and 3 still pending]"
)
→ "Saved. Project: Booking System (abc-123), Summary: sum-002"

Graph:
  (Project:abc-123)-[:HAS_LATEST_SUMMARY]->(Summary:sum-002)
  (Summary:sum-002)-[:PREVIOUS_VERSION]->(Summary:sum-001)
```

*End of Session 1.*

### Session 2 — "Fetch from the booking system and continue coding."

```
AI → Prompts: "New project or continuing existing?"
User → "Continuing the booking system"

Route A: Hybrid rerank search triggered.

hybrid_rerank_search("booking system continue coding")
→ top result: node_id sum-002, cross_score: 0.91

get_project_node("sum-002")
→ {project_id: "abc-123", project_name: "Booking System", question: "Create a plan for my booking system"}

get_latest_summary_tool("abc-123") → sum-002 text
Continue coding.

upsert_project_tool(
    project_id="abc-123",
    name="Booking System",            # Keep existing name
    question="Create a plan for my booking system",
    summary="[delta: phase 2 implemented. phase 3 still pending]"
)
→ "Saved. Project: Booking System (abc-123), Summary: sum-003"

Graph:
  (Project:abc-123)-[:HAS_LATEST_SUMMARY]->(Summary:sum-003)
  (Summary:sum-003)-[:PREVIOUS_VERSION]->(Summary:sum-002)-[:PREVIOUS_VERSION]->(Summary:sum-001)
```

### Session 3 — "New project: Hotel management system. Reuse the database design from the booking system."

```
AI → Prompts: "New project or continuing existing? Any reuse?"
User → "New project, reuse database from booking system"

Route C: Targeted hybrid rerank search on database design.

hybrid_rerank_search("database design booking system", top_k=9, rrf_k=60)
→ top result: node_id sum-003

get_project_node("sum-003")
→ {project_id: "abc-123", project_name: "Booking System", question: "Create a plan for my booking system"}

get_latest_summary_tool("abc-123") → extract DB schema section

Generate hotel system using booking system's DB as a base.

upsert_project_tool(
    name="Hotel Management",          # New project's own name
    question="Create a hotel management system",
    summary="[initial: DB design reused from project abc-123 (Booking System). Hotel-specific features pending]"
    # No project_id — brand new project
)
→ "Saved. Project: Hotel Management (xyz-789), Summary: sum-hotel-001"
```

---

## Summary Content Guidelines

Each summary saved to Neo4j is a **focused delta** — it captures what happened in this task/session and the current status going forward. Since summaries are linked via `PREVIOUS_VERSION`, there is no need to repeat content from prior summaries.

A good summary includes:

1. **What Was Done** — actions taken in this session (implemented features, files created/modified, decisions made)
2. **Current State** — what exists and works right now as a result of this session
3. **Technologies / Approaches Used** — any new libraries, patterns, or design decisions introduced this session
4. **Files Affected** — file names created or modified
5. **What Remains** — pending work, known issues, next steps
6. **References** *(if applicable)* — if parts were borrowed from another project, note the source project ID and what was reused

**Keep summaries focused and concise.** The history is preserved across the node chain — each summary only needs to describe its own contribution and the current status, not re-document everything from scratch.

---

## Decision Flowchart

```
┌─────────────────────────────────────────────┐
│ 1. Receive user request                     │
└─────────────┬───────────────────────────────┘
              ▼
┌─────────────────────────────────────────────┐
│ 2. ALWAYS PROMPT (unless user already       │
│    answered in their message):              │
│    a) New project or continuing existing?   │
│    b) If new: reuse any part from previous? │
└────┬────────────┬───────────────┬───────────┘
     │            │               │
     ▼            ▼               ▼
[CONTINUE]  [NEW, NO REUSE]  [NEW + REUSE PART]
     │            │               │
     ▼            ▼               ▼
Hybrid rerank Skip search    Targeted hybrid
search on     entirely       rerank search on
user query                   that specific part
     │            │               │
     ▼            ▼               ▼
Load latest   Generate       Load reference
summary       fresh          summary, extract
              project        relevant section
     └────────────┴───────────────┘
                  ▼
     ┌────────────────────────────┐
     │ Generate code / content    │
     └────────────┬───────────────┘
                  ▼
     ┌────────────────────────────┐
     │ Create DELTA summary       │
     │ (this session only)        │
     └────────────┬───────────────┘
                  ▼
     ┌────────────────────────────┐
     │ upsert_project_tool()      │
     │ Continue → include         │
     │   project_id (chains)      │
     │ New → omit project_id      │
     └────────────┬───────────────┘
                  ▼
     ┌────────────────────────────┐
     │ Save files to filesystem   │
     │ Update MODIFIED_FILES.md   │
     └────────────────────────────┘
```

---

## Tool Reference

### `hybrid_rerank_search`
```python
results = hybrid_rerank_search(
    query_text="booking system database",
    top_k=9,
    rrf_k=60
)
# Returns: [{'id': str, 'question': str, 'summary': str, 'cross_score': float}, ...]
# Uses BM25 + semantic RRF fusion, then cross-encoder reranking for best relevance
# Note: 'id' here is the Summary node ID — use get_project_node() to resolve the Project
```

### `get_project_node`
```python
project = get_project_node(summary_id="sum-002")
# Returns: {'project_id': str, 'project_name': str, 'question': str, 'updated_at': str}
# Use this after hybrid_rerank_search to resolve the Project node from a Summary node ID
```

### `get_data_tool`
```python
projects = get_data_tool(node_ids=["sum-001", "sum-002"])
# Returns: [{'id': str, 'question': str, 'summary': str}, ...]
```

### `get_latest_summary_tool`
```python
latest = get_latest_summary_tool(project_id="abc-123")
# Returns: {'project_id': str, 'project_name': str, 'latest_summary': str, 'summary_id': str}
```

### `upsert_project_tool`
```python
# New project — omit project_id, derive a short readable name
result = upsert_project_tool(
    name="Hotel Management",       # Short, human-readable name for this project
    question="Build a hotel system",
    summary="[delta summary for this session]"
)

# Continue existing project — include project_id and keep existing name unchanged
result = upsert_project_tool(
    project_id="abc-123",
    name="Booking System",         # Keep existing name — do NOT rename
    question="Build a booking system",   # Never change original question
    summary="[delta summary for this session]"
)
# Returns: "Saved. Project: {project_name} ({project_id}), Summary: {summary_id}"
```

### `delete_project_tool`
```python
# ONLY when user explicitly says delete / remove / forget
delete_project_tool(project_id="abc-123")
# Returns: True
```

---

## Guardrails

- **Always prompt first**: Ask new-or-existing AND reuse questions BEFORE any search or generation
- **Never auto-search for new projects without reuse**: Route B skips search entirely
- **Never skip the prompt**: Confirm explicitly even if user's message implies an answer — unless they already clearly answered both questions
- **Don't re-prompt within same session**: Once `project_id` is established, continue directly on follow-up queries
- **Reuse = new project**: Borrowing a part from another project creates a brand NEW project, never continues the old one
- **Delta summaries**: Each summary covers only this session's work and current status — history lives in the node chain
- **Always pass name**: `upsert_project_tool` requires `name` — derive a short readable one for new projects, keep existing one unchanged for continuations
- **Never rename existing projects**: When updating a project, always pass back the same `name` retrieved from `get_project_node`
- **Resolve via get_project_node**: After `hybrid_rerank_search`, always call `get_project_node(summary_id)` to get the authoritative `project_id` and `name` before calling `get_latest_summary_tool` or `upsert_project_tool`
- **Never change original question**: Always pass the original `project_question` when updating an existing project
- **History is automatic**: `upsert_project_tool` handles `PREVIOUS_VERSION` chaining — do not manage it manually
- **Track every file**: Always update `MODIFIED_FILES.md` after each task
- **Use hybrid_rerank_search for all searches**: It combines BM25 + semantic + cross-encoder reranking for best results

---

## Troubleshooting

**AI skipped the prompt and jumped straight to search or generation**
→ This violates the Prompt-First rule. Always ask new-or-existing before any action.

**Semantic search ran even though user said "new project, no reuse"**
→ Route B must skip search entirely. Only Routes A and C trigger search.

**Can't find previous project during continuation**
→ Use natural language in `hybrid_rerank_search` — it's meaning + keyword based. If still not found, ask user for project ID directly.

**Reuse case created a new summary on the old project instead of a new project**
→ Route C must omit `project_id` in `upsert_project_tool`. Reuse = reference only, not continuation.

**`get_latest_summary_tool` returns "No project found"**
→ Project ID is wrong. Call `get_project_node(summary_id)` using the summary ID from `hybrid_rerank_search` to re-derive the correct `project_id`.

**`upsert_project_tool` fails or project name is wrong after continuation**
→ Always call `get_project_node(summary_id)` after search to get the correct `project_name` before calling `upsert_project_tool`. Never guess or derive the name manually.

**hybrid_rerank_search returns no results for obvious matches**
→ Check embeddings exist and BM25 full-text index is created. Verify Neo4j is accessible at localhost:7687 and vector index is set up.

**MCP tool errors / no response**
→ Verify MCP server is running and Neo4j is accessible at localhost:7687. Ensure both vector index and full-text index exist.