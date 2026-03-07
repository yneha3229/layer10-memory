"""
build_graph.py
--------------
Phase 5: Memory Graph

Loads all deduped data and builds:
1. SQLite database (queryable, persistent store)
2. NetworkX graph (for traversal and visualization)
3. Exports graph to JSON for visualization layer

Usage:
    python build_graph.py

Output:
    data/graph/memory.db          - SQLite database
    data/graph/graph.json         - NetworkX graph export
    data/graph/graph_stats.json   - graph statistics
"""

import json
import sqlite3
import time
from pathlib import Path
import networkx as nx

# ── Directories ─────────────────────────────────────────────────────────────
DATA_DIR  = Path("data")
DEDUPED   = DATA_DIR / "deduped"
GRAPH_DIR = DATA_DIR / "graph"
GRAPH_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH   = GRAPH_DIR / "memory.db"


# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — Set up SQLite database
# ════════════════════════════════════════════════════════════════════════════

def create_database(conn: sqlite3.Connection):
    """
    Creates all tables in the SQLite database.
    This is our queryable, persistent memory store.
    """
    cursor = conn.cursor()

    # Entities table (persons + components + issues)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id              TEXT PRIMARY KEY,
            entity_type     TEXT NOT NULL,
            canonical_name  TEXT NOT NULL,
            aliases         TEXT,         -- JSON array
            mention_count   INTEGER DEFAULT 1,
            seen_in_issues  TEXT,         -- JSON array
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Claims table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS claims (
            id              TEXT PRIMARY KEY,
            claim_type      TEXT NOT NULL,
            subject         TEXT NOT NULL,
            object          TEXT NOT NULL,
            confidence      REAL DEFAULT 0.5,
            support_count   INTEGER DEFAULT 1,
            evidence_list   TEXT,         -- JSON array
            seen_in_issues  TEXT,         -- JSON array
            is_current      INTEGER DEFAULT 1,
            valid_from      TEXT,
            valid_to        TEXT,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Evidence table (grounding — every claim points here)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS evidence (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id    TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            excerpt     TEXT,
            timestamp   TEXT,
            FOREIGN KEY (claim_id) REFERENCES claims(id)
        )
    """)

    # Issues table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS issues (
            id              INTEGER PRIMARY KEY,
            title           TEXT,
            state           TEXT,
            url             TEXT,
            extracted_at    TEXT
        )
    """)

    # Conflicts table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conflicts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conflict_type   TEXT,
            issue_number    INTEGER,
            description     TEXT,
            history         TEXT,         -- JSON array
            current_state   TEXT,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Merge log table (audit trail)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS merge_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            merge_type      TEXT,
            merged_from     TEXT,
            merged_into     TEXT,
            reason          TEXT,
            timestamp       TEXT,
            undone          INTEGER DEFAULT 0
        )
    """)

    # Full text search index on claims
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts
        USING fts5(claim_id, claim_type, subject, object)
    """)

    conn.commit()
    print("  ✅ Database schema created")


# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — Load entities into database
# ════════════════════════════════════════════════════════════════════════════

def load_entities(conn: sqlite3.Connection) -> int:
    """Load persons and components into the entities table."""
    cursor = conn.cursor()

    entities_data = json.loads(
        (DEDUPED / "canonical_entities.json").read_text(encoding="utf-8")
    )

    count = 0
    for person in entities_data["persons"]:
        cursor.execute("""
            INSERT OR REPLACE INTO entities
            (id, entity_type, canonical_name, aliases, mention_count, seen_in_issues)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            f"person:{person['canonical_id']}",
            "Person",
            person["canonical_id"],
            json.dumps(person.get("aliases", [])),
            person.get("mention_count", 1),
            json.dumps(person.get("seen_in_issues", [])),
        ))
        count += 1

    for comp in entities_data["components"]:
        cursor.execute("""
            INSERT OR REPLACE INTO entities
            (id, entity_type, canonical_name, aliases, mention_count, seen_in_issues)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            f"component:{comp['canonical_id']}",
            "Component",
            comp["canonical_id"],
            json.dumps(comp.get("aliases", [])),
            comp.get("mention_count", 1),
            json.dumps(comp.get("seen_in_issues", [])),
        ))
        count += 1

    conn.commit()
    print(f"  ✅ Loaded {count} entities")
    return count


# ════════════════════════════════════════════════════════════════════════════
# STEP 3 — Load issues into database
# ════════════════════════════════════════════════════════════════════════════

def load_issues(conn: sqlite3.Connection) -> int:
    """Load issue metadata into the issues table."""
    cursor = conn.cursor()
    extracted_dir = DATA_DIR / "extracted"
    count = 0

    for f in sorted(extracted_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            cursor.execute("""
                INSERT OR REPLACE INTO issues
                (id, title, state, url, extracted_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                data["issue_number"],
                data["issue_title"],
                data["issue_state"],
                data["issue_url"],
                data["extracted_at"],
            ))
            count += 1
        except Exception:
            continue

    conn.commit()
    print(f"  ✅ Loaded {count} issues")
    return count


# ════════════════════════════════════════════════════════════════════════════
# STEP 4 — Load claims + evidence into database
# ════════════════════════════════════════════════════════════════════════════

def load_claims(conn: sqlite3.Connection) -> int:
    """Load claims and their evidence into the database."""
    cursor = conn.cursor()

    claims_data = json.loads(
        (DEDUPED / "canonical_claims.json").read_text(encoding="utf-8")
    )

    count = 0
    for claim in claims_data["claims"]:
        claim_id = claim["claim_id"]

        # Insert claim
        cursor.execute("""
            INSERT OR REPLACE INTO claims
            (id, claim_type, subject, object, confidence,
             support_count, evidence_list, seen_in_issues,
             is_current, valid_from, valid_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            claim_id,
            claim["claim_type"],
            claim["subject"],
            claim["object"],
            claim["confidence"],
            claim["support_count"],
            json.dumps(claim.get("evidence_list", [])),
            json.dumps(claim.get("seen_in_issues", [])),
            1 if claim.get("is_current", True) else 0,
            claim.get("valid_from", ""),
            claim.get("valid_to", None),
        ))

        # Insert each evidence item separately
        for ev in claim.get("evidence_list", []):
            cursor.execute("""
                INSERT INTO evidence
                (claim_id, source_id, excerpt, timestamp)
                VALUES (?, ?, ?, ?)
            """, (
                claim_id,
                ev.get("source_id", ""),
                ev.get("excerpt", ""),
                ev.get("timestamp", ""),
            ))

        # Add to full text search index
        cursor.execute("""
            INSERT OR REPLACE INTO claims_fts
            (claim_id, claim_type, subject, object)
            VALUES (?, ?, ?, ?)
        """, (
            claim_id,
            claim["claim_type"],
            claim["subject"],
            claim["object"],
        ))

        count += 1

    conn.commit()
    print(f"  ✅ Loaded {count} claims with evidence")
    return count


# ════════════════════════════════════════════════════════════════════════════
# STEP 5 — Load conflicts + merge log
# ════════════════════════════════════════════════════════════════════════════

def load_conflicts_and_merges(conn: sqlite3.Connection):
    """Load conflicts and merge log into database."""
    cursor = conn.cursor()

    # Conflicts
    conflicts_data = json.loads(
        (DEDUPED / "conflicts.json").read_text(encoding="utf-8")
    )
    for conflict in conflicts_data["conflicts"]:
        cursor.execute("""
            INSERT INTO conflicts
            (conflict_type, issue_number, description, history, current_state)
            VALUES (?, ?, ?, ?, ?)
        """, (
            conflict["conflict_type"],
            conflict["issue_number"],
            conflict["description"],
            json.dumps(conflict.get("history", [])),
            conflict.get("current_state", ""),
        ))

    # Merge log
    merge_data = json.loads(
        (DEDUPED / "merge_log.json").read_text(encoding="utf-8")
    )
    for merge in merge_data["merges"]:
        cursor.execute("""
            INSERT INTO merge_log
            (merge_type, merged_from, merged_into, reason, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (
            merge.get("type", ""),
            merge.get("merged_from", ""),
            merge.get("merged_into", ""),
            merge.get("reason", ""),
            merge.get("timestamp", ""),
        ))

    conn.commit()
    print(f"  ✅ Loaded {len(conflicts_data['conflicts'])} conflicts")
    print(f"  ✅ Loaded {len(merge_data['merges'])} merge records")


# ════════════════════════════════════════════════════════════════════════════
# STEP 6 — Build NetworkX graph
# ════════════════════════════════════════════════════════════════════════════

def build_networkx_graph(conn: sqlite3.Connection) -> nx.DiGraph:
    """
    Builds a directed graph using NetworkX.
    Nodes = entities (persons, components, issues)
    Edges = claims (relationships between entities)
    """
    cursor = conn.cursor()
    G = nx.DiGraph()

    # Add entity nodes
    cursor.execute("SELECT id, entity_type, canonical_name, mention_count FROM entities")
    for row in cursor.fetchall():
        entity_id, entity_type, name, count = row
        G.add_node(entity_id,
                   label=name,
                   type=entity_type,
                   mention_count=count)

    # Add issue nodes
    cursor.execute("SELECT id, title, state FROM issues")
    for row in cursor.fetchall():
        issue_id, title, state = row
        G.add_node(f"issue:{issue_id}",
                   label=f"#{issue_id}: {title[:50]}",
                   type="Issue",
                   state=state)

    # Add claim edges
    cursor.execute("""
        SELECT id, claim_type, subject, object, confidence, support_count
        FROM claims WHERE is_current = 1
    """)
    for row in cursor.fetchall():
        claim_id, claim_type, subject, obj, confidence, support = row

        # Resolve subject node
        subject_node = resolve_node(G, subject)
        object_node  = resolve_node(G, obj)

        if subject_node and object_node:
            G.add_edge(subject_node, object_node,
                       claim_id=claim_id,
                       claim_type=claim_type,
                       confidence=confidence,
                       support_count=support,
                       label=claim_type)

    print(f"  ✅ Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    return G


def resolve_node(G: nx.DiGraph, identifier: str) -> str | None:
    """
    Tries to find the right node ID for a given identifier.
    Handles: issue/123, username, component name.
    """
    # Direct match
    if identifier in G:
        return identifier

    # Try as person
    person_id = f"person:{identifier}"
    if person_id in G:
        return person_id

    # Try as component
    comp_id = f"component:{identifier}"
    if comp_id in G:
        return comp_id

    # Try as issue number
    if identifier.startswith("issue/"):
        num = identifier.split("/")[-1]
        issue_id = f"issue:{num}"
        if issue_id in G:
            return issue_id

    return None


# ════════════════════════════════════════════════════════════════════════════
# STEP 7 — Export graph to JSON for visualization
# ════════════════════════════════════════════════════════════════════════════

def export_graph(G: nx.DiGraph):
    """
    Exports the graph to a JSON format suitable for visualization.
    Format compatible with vis.js / pyvis.
    """
    # Color scheme by node type
    colors = {
        "Person":    "#4A90E2",   # blue
        "Component": "#7ED321",   # green
        "Issue":     "#F5A623",   # orange
    }

    nodes = []
    for node_id, data in G.nodes(data=True):
        node_type = data.get("type", "Unknown")
        nodes.append({
            "id":            node_id,
            "label":         data.get("label", node_id)[:40],
            "type":          node_type,
            "color":         colors.get(node_type, "#999999"),
            "mention_count": data.get("mention_count", 1),
            "state":         data.get("state", ""),
        })

    edges = []
    for src, dst, data in G.edges(data=True):
        edges.append({
            "from":          src,
            "to":            dst,
            "claim_id":      data.get("claim_id", ""),
            "claim_type":    data.get("claim_type", ""),
            "label":         data.get("label", ""),
            "confidence":    data.get("confidence", 0.5),
            "support_count": data.get("support_count", 1),
        })

    graph_export = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nodes":        nodes,
        "edges":        edges,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "persons":     sum(1 for n in nodes if n["type"] == "Person"),
            "components":  sum(1 for n in nodes if n["type"] == "Component"),
            "issues":      sum(1 for n in nodes if n["type"] == "Issue"),
        }
    }

    (GRAPH_DIR / "graph.json").write_text(
        json.dumps(graph_export, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    # Save stats separately
    (GRAPH_DIR / "graph_stats.json").write_text(
        json.dumps(graph_export["stats"], indent=2),
        encoding="utf-8"
    )

    print(f"  ✅ Graph exported to data/graph/graph.json")
    print(f"     Nodes: {len(nodes)} | Edges: {len(edges)}")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Layer10 — Memory Graph Builder")
    print("=" * 60)

    # Remove old database if exists
    if DB_PATH.exists():
        DB_PATH.unlink()
        print("  🗑️  Removed old database")

    # Connect to SQLite
    conn = sqlite3.connect(DB_PATH)

    print("\n📐 Creating database schema...")
    create_database(conn)

    print("\n📦 Loading entities...")
    load_entities(conn)

    print("\n📋 Loading issues...")
    load_issues(conn)

    print("\n🔗 Loading claims + evidence...")
    load_claims(conn)

    print("\n⚡ Loading conflicts + merge log...")
    load_conflicts_and_merges(conn)

    print("\n🕸️  Building NetworkX graph...")
    G = build_networkx_graph(conn)

    print("\n💾 Exporting graph...")
    export_graph(G)

    conn.close()

    print("\n" + "=" * 60)
    print("  Memory Graph Complete!")
    print(f"  📁 Database: data/graph/memory.db")
    print(f"  📁 Graph:    data/graph/graph.json")
    print("  Next step: python retrieval.py")
    print("=" * 60)