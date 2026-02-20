from mcp.server.fastmcp import FastMCP
from neo4j import GraphDatabase
from sentence_transformers import SentenceTransformer, CrossEncoder
import uuid

# ------------------------
# CREATE INDEXS
# ------------------------

# CREATE FULLTEXT INDEX project_summary_fulltext_index FOR (n:Summary) ON EACH [n.text]
# CREATE VECTOR INDEX project_embedding_index FOR (n:Summary) ON (n.embedding)
# OPTIONS {indexConfig: {
#  `vector.dimensions`: 768,
#  `vector.similarity_function`: 'cosine'
# }}

# ------------------------
# CONFIG
# ------------------------
# Embedding model
model = SentenceTransformer("all-mpnet-base-v2")
cross_encoder_model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

# Neo4j connection
uri = "neo4j://localhost:7687"
driver = GraphDatabase.driver(uri, auth=("neo4j", "password"))

# MCP server
mcp = FastMCP(
    name="Neo4jProjectMCP",
)

# ------------------------
# UPSERT PROJECT (With History Chaining)
# ------------------------
@mcp.tool(
    description="Creates or updates a project with a readable name. Links the new summary to the previous one."
)
def upsert_project_tool(name: str, question: str, summary: str, project_id: str = None) -> str:
    if not project_id:
        project_id = str(uuid.uuid4())

    text_for_embedding = f"{question}\n{summary}"
    embedding = model.encode(text_for_embedding).tolist()
    summary_id = str(uuid.uuid4())

    # Cypher Logic updated to include 'name'
    cypher = """
    MERGE (p:Project {id: $project_id})
    SET p.name = $name, 
        p.question = $question, 
        p.updated_at = datetime()

    WITH p
    OPTIONAL MATCH (p)-[old_rel:HAS_LATEST_SUMMARY]->(old_s:Summary)
    DELETE old_rel

    CREATE (s:Summary {id: $summary_id})
    SET s.text = $summary,
        s.embedding = $embedding,
        s.created_at = datetime()

    MERGE (p)-[:HAS_LATEST_SUMMARY]->(s)
    MERGE (p)-[:HAS_SUMMARY]->(s)

    WITH p, s, old_s
    FOREACH (_ IN CASE WHEN old_s IS NOT NULL THEN [1] ELSE [] END |
        MERGE (s)-[:PREVIOUS_VERSION]->(old_s)
    )

    RETURN p.id AS project_id, p.name AS project_name, s.id AS summary_id
    """

    with driver.session() as session:
        result = session.run(
            cypher,
            project_id=project_id,
            name=name,
            question=question,
            summary=summary,
            embedding=embedding,
            summary_id=summary_id,
        )
        record = result.single()
        return f"Saved. Project: {record['project_name']} ({record['project_id']}), Summary: {record['summary_id']}"

# ------------------------
# GET PROJECT NODE
# ------------------------
@mcp.tool(
    description="Fetches the Project node for a given Summary node ID."
)
def get_project_node(summary_id: str):
    """
    Args:
        summary_id: The ID of a Summary node
    Returns:
        Project node dictionary (id, name, question, updated_at) or None
    """
    cypher = """
    MATCH (p:Project)-[:HAS_SUMMARY]->(s:Summary {id: $summary_id})
    RETURN p.id AS project_id, p.name AS project_name, p.question AS question, p.updated_at AS updated_at
    """
    with driver.session() as session:
        record = session.run(cypher, summary_id=summary_id).single()
        return record.data() if record else None
    
# ------------------------
# GET LATEST STATE
# ------------------------
@mcp.tool(
    description="Fetches the single most recent summary for a specific project ID. Use this to 'resume' a project."
)
def get_latest_summary_tool(project_id: str):
    cypher = """
    MATCH (p:Project {id: $project_id})-[:HAS_LATEST_SUMMARY]->(s:Summary)
    RETURN p.id AS project_id, p.question AS project_name, s.text AS latest_summary, s.id AS summary_id
    """
    with driver.session() as session:
        result = session.run(cypher, project_id=project_id)
        record = result.single()
        return record.data() if record else "No project found."

# ------------------------
# SEMANTIC SEARCH
# ------------------------
def get_semantic_matches(query_text: str, top_k: int = 9, min_score: float = 0.35):
    """
    Performs a semantic search on the embeddings and returns matching nodes with scores.
    """
    query_embedding = model.encode(query_text).tolist()
    cypher = """
    CALL db.index.vector.queryNodes('project_embedding_index', $top_k, $embedding)
    YIELD node, score
    WHERE score >= $min_score
    RETURN node.id AS node_id, node.text AS summary, score
    """
    with driver.session() as session:
        result = session.run(
            cypher,
            top_k=top_k,
            embedding=query_embedding,
            min_score=min_score
        )
        return [record.data() for record in result]
        

@mcp.tool(
    description="Fetches all summaries linked to a project via HAS_SUMMARY using the Project node ID. Returns project info and historical summaries."
)
def get_data_tool(node_ids: list):
    """
    Fetches project information for the given embedding node IDs.
    """
    if not node_ids:
        return []

    cypher = """
    MATCH (p:Project)-[:HAS_SUMMARY]->(n)
    WHERE n.id IN $node_ids
    RETURN p.id AS id, p.question AS question, n.text AS summary
    """
    with driver.session() as session:
        result = session.run(cypher, node_ids=node_ids)
        return [record.data() for record in result]

# ------------------------
# BM25 FULL-TEXT SEARCH
# ------------------------
def get_bm25_matches(query_text: str, top_k: int = 10, min_score: float = 0.0):
    """
    Performs BM25 keyword-based search using Neo4j full-text index.
    """
    cypher = """
    CALL db.index.fulltext.queryNodes(
        'project_summary_fulltext_index',
        $query_text
    )
    YIELD node, score
    WHERE score >= $min_score
    RETURN node.id AS node_id,
           node.text AS summary,
           score
    ORDER BY score DESC
    LIMIT $top_k
    """

    with driver.session() as session:
        result = session.run(
            cypher,
            query_text=query_text,
            top_k=top_k,
            min_score=min_score
        )
        return [record.data() for record in result]

# ------------------------
# HYBRID SEARCH (RRF FUSION)
# ------------------------
def hybrid_rrf_search(query_text: str, top_k: int = 197, rrf_k: int = 60):
    """
    Combines BM25 + semantic search results using Reciprocal Rank Fusion.
    """
    # ---- Step 1: Get semantic results ----
    semantic_results = get_semantic_matches(
        query_text=query_text,
        top_k=top_k * 2,
        min_score=0.0
    )
    # ---- Step 2: Get BM25 results ----
    bm25_results = get_bm25_matches(
        query_text=query_text,
        top_k=top_k * 2,
        min_score=0.0
    )
    # ---- Step 3: Rank maps ----
    rrf_scores = {}
    # Semantic ranking contribution
    for rank, item in enumerate(semantic_results, start=1):
        node_id = item["node_id"]
        rrf_scores.setdefault(node_id, 0)
        rrf_scores[node_id] += 1 / (rrf_k + rank)
    # BM25 ranking contribution
    for rank, item in enumerate(bm25_results, start=1):
        node_id = item["node_id"]
        rrf_scores.setdefault(node_id, 0)
        rrf_scores[node_id] += 1 / (rrf_k + rank)
    # ---- Step 4: Sort by RRF score ----
    ranked_ids = sorted(
        rrf_scores.items(),
        key=lambda x: x[1],
        reverse=True
    )
    # Keep top_k
    top_ranked_ids = [node_id for node_id, _ in ranked_ids[:top_k]]
    # ---- Step 5: Fetch project data ----
    return get_data_tool(top_ranked_ids)

# ------------------------
# HYBRID SEARCH (RRF FUSION) + CROSS-ENCODER
# ------------------------
@mcp.tool(
    description="Performs hybrid search using BM25 and semantic vector search with Reciprocal Rank Fusion (RRF) for candidate ranking, then reranks the top results using a Cross-Encoder neural model for precise relevance scoring."
)
def hybrid_rerank_search(
    query_text: str,
    top_k: int = 197,
    rrf_k: int = 60
):
    """
    1. Get candidates via BM25 + semantic RRF fusion
    2. Re-rank using cross-encoder neural model
    """

    # --- 1️⃣ Candidate retrieval with RRF ---
    candidates = hybrid_rrf_search(query_text, top_k=top_k*3, rrf_k=rrf_k)

    if not candidates:
        return []

    # --- 2️⃣ Prepare pairs for cross-encoder ---
    query_doc_pairs = [(query_text, c["summary"]) for c in candidates]

    # --- 3️⃣ Predict relevance scores ---
    scores = cross_encoder_model.predict(query_doc_pairs)

    # --- 4️⃣ Assign cross-encoder scores ---
    for c, s in zip(candidates, scores):
        c["cross_score"] = s

    # --- 5️⃣ Sort by cross-encoder score ---
    reranked_results = sorted(
        candidates,
        key=lambda x: x["cross_score"],
        reverse=True
    )

    # Return top_k results
    return reranked_results[:top_k]

# ------------------------
# DELETE PROJECT
# ------------------------
@mcp.tool(
    description="Deletes a project and its summary. Use this only when specifically asked to 'delete', 'remove', or 'forget' a project by its ID."
)
def delete_project_tool(project_id: str) -> bool:
    """
    Args:
        project_id: The unique ID of the project to remove.
    """
    cypher = "MATCH (p:Project {id: $project_id}) DETACH DELETE p"
    with driver.session() as session:
        session.run(cypher, project_id=project_id)
    return True

# ------------------------
# START MCP SERVER
# ------------------------
if __name__ == "__main__":
    # Run over stdio (for AI agent integration) or change to SSE/HTTP if you want remote access
    mcp.run()