"""
visualize.py
------------
Phase 7: Visualization Layer

Builds an interactive HTML graph visualization using PyVis.
Opens in any web browser - no server needed.

Usage:
    python visualize.py

Output:
    data/visualization/graph.html          - main interactive graph
    data/visualization/evidence_panel.html - evidence browser
"""

import json
import sqlite3
from pathlib import Path
from pyvis.network import Network

# ── Directories ──────────────────────────────────────────────────────────────
DATA_DIR   = Path("data")
GRAPH_DIR  = DATA_DIR / "graph"
DEDUPED    = DATA_DIR / "deduped"
VIZ_DIR    = DATA_DIR / "visualization"
VIZ_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH    = GRAPH_DIR / "memory.db"


# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — Build Main Interactive Graph
# ════════════════════════════════════════════════════════════════════════════

def build_main_graph():
    """
    Builds the main interactive graph visualization.
    - Blue nodes  = Persons
    - Green nodes = Components
    - Orange nodes = Issues
    - Edges = Claims/relationships
    """
    print("\n🎨 Building main graph visualization...")

    conn   = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Create PyVis network
    net = Network(
        height="750px",
        width="100%",
        bgcolor="#1a1a2e",
        font_color="white",
        directed=True,
    )

    # Physics settings for better layout
    net.set_options("""
    {
      "physics": {
        "forceAtlas2Based": {
          "gravitationalConstant": -50,
          "centralGravity": 0.01,
          "springLength": 150,
          "springConstant": 0.08
        },
        "solver": "forceAtlas2Based",
        "stabilization": { "iterations": 100 }
      },
      "interaction": {
        "hover": true,
        "tooltipDelay": 100
      },
      "edges": {
        "smooth": { "type": "continuous" },
        "arrows": { "to": { "enabled": true, "scaleFactor": 0.5 } }
      }
    }
    """)

    # ── Add Person nodes (top 80 by mention count) ───────────────────────────
    cursor.execute("""
        SELECT id, canonical_name, mention_count, aliases, seen_in_issues
        FROM entities
        WHERE entity_type = 'Person'
        ORDER BY mention_count DESC
        LIMIT 80
    """)
    persons = cursor.fetchall()
    print(f"  Adding {len(persons)} person nodes...")

    for p in persons:
        aliases = json.loads(p["aliases"] or "[]")
        issues  = json.loads(p["seen_in_issues"] or "[]")
        tooltip = (
            f"👤 {p['canonical_name']}\n"
            f"Mentioned in {p['mention_count']} issues\n"
            f"Issues: {', '.join(f'#{i}' for i in issues[:5])}"
            f"{'...' if len(issues) > 5 else ''}\n"
            f"Aliases: {', '.join(aliases[:3]) if aliases else 'none'}"
        )
        # Size by mention count (more mentions = bigger node)
        size = min(10 + p["mention_count"] * 2, 50)

        net.add_node(
            p["id"],
            label=p["canonical_name"],
            title=tooltip,
            color="#4A90E2",
            size=size,
            shape="dot",
            font={"size": 12, "color": "white"},
        )

    # ── Add Component nodes (top 60 by mention count) ────────────────────────
    cursor.execute("""
        SELECT id, canonical_name, mention_count, aliases, seen_in_issues
        FROM entities
        WHERE entity_type = 'Component'
        ORDER BY mention_count DESC
        LIMIT 60
    """)
    components = cursor.fetchall()
    print(f"  Adding {len(components)} component nodes...")

    for c in components:
        aliases = json.loads(c["aliases"] or "[]")
        issues  = json.loads(c["seen_in_issues"] or "[]")
        tooltip = (
            f"🔧 {c['canonical_name']}\n"
            f"Mentioned in {c['mention_count']} issues\n"
            f"Issues: {', '.join(f'#{i}' for i in issues[:5])}"
            f"{'...' if len(issues) > 5 else ''}\n"
            f"Aliases: {', '.join(aliases[:3]) if aliases else 'none'}"
        )
        size = min(10 + c["mention_count"], 45)

        net.add_node(
            c["id"],
            label=c["canonical_name"],
            title=tooltip,
            color="#7ED321",
            size=size,
            shape="diamond",
            font={"size": 12, "color": "white"},
        )

    # ── Add Issue nodes (top 60 most connected) ──────────────────────────────
    cursor.execute("""
        SELECT i.id, i.title, i.state, i.url
        FROM issues i
        JOIN claims c ON (
            c.subject LIKE '%issue/' || i.id || '%'
            OR c.object LIKE '%issue/' || i.id || '%'
        )
        GROUP BY i.id
        ORDER BY COUNT(c.id) DESC
        LIMIT 60
    """)
    issues = cursor.fetchall()
    print(f"  Adding {len(issues)} issue nodes...")

    for iss in issues:
        state_color = "#F5A623" if iss["state"] == "open" else "#E74C3C"
        tooltip = (
            f"📋 Issue #{iss['id']}\n"
            f"{iss['title']}\n"
            f"State: {iss['state']}\n"
            f"URL: {iss['url']}"
        )
        net.add_node(
            f"issue:{iss['id']}",
            label=f"#{iss['id']}",
            title=tooltip,
            color=state_color,
            size=15,
            shape="box",
            font={"size": 10, "color": "white"},
        )

    # ── Add edges (claims) ───────────────────────────────────────────────────
    cursor.execute("""
        SELECT id, claim_type, subject, object, confidence, support_count
        FROM claims
        WHERE is_current = 1
        AND confidence >= 0.7
        ORDER BY support_count DESC
        LIMIT 500
    """)
    claims = cursor.fetchall()
    print(f"  Adding {len(claims)} claim edges...")

    # Edge colors by claim type
    edge_colors = {
        "REPORTED_BY":   "#4A90E2",
        "ASSIGNED_TO":   "#F5A623",
        "AFFECTS":       "#E74C3C",
        "DECIDED":       "#9B59B6",
        "RELATES_TO":    "#1ABC9C",
        "COMMENTED_ON":  "#95A5A6",
    }

    added_edges = 0
    node_ids = set(net.get_nodes())

    for claim in claims:
        subj = resolve_node_id(claim["subject"])
        obj  = resolve_node_id(claim["object"])

        # Only add edge if both nodes exist in graph
        if subj in node_ids and obj in node_ids:
            color  = edge_colors.get(claim["claim_type"], "#888888")
            width  = min(1 + claim["support_count"] * 0.5, 5)
            tooltip = (
                f"{claim['claim_type']}\n"
                f"{claim['subject']} → {claim['object']}\n"
                f"Confidence: {claim['confidence']:.0%}\n"
                f"Supported by {claim['support_count']} source(s)"
            )
            net.add_edge(
                subj, obj,
                title=tooltip,
                label=claim["claim_type"],
                color=color,
                width=width,
                font={"size": 8, "color": "#cccccc"},
            )
            added_edges += 1

    print(f"  Added {added_edges} edges")

    # Save graph
    output_path = str(VIZ_DIR / "graph.html")
    net.save_graph(output_path)

    # Inject legend into HTML
    inject_legend(output_path)

    conn.close()
    print(f"  ✅ Main graph saved: {output_path}")
    return output_path


def resolve_node_id(identifier: str) -> str:
    """Convert claim subject/object to graph node ID."""
    if identifier.startswith("issue/"):
        num = identifier.split("/")[-1]
        return f"issue:{num}"
    if "/" not in identifier and not identifier.startswith("component:"):
        return f"person:{identifier}"
    return identifier


def inject_legend(html_path: str):
    """Injects a legend and title into the graph HTML."""
    legend_html = """
    <div style="position:fixed; top:10px; left:10px; background:#1a1a2e;
                border:1px solid #444; border-radius:8px; padding:15px;
                color:white; font-family:Arial; font-size:13px; z-index:1000;">
        <b style="font-size:15px;">🧠 Layer10 Memory Graph</b><br><br>
        <b>Nodes:</b><br>
        🔵 Person &nbsp;&nbsp;&nbsp;
        🟢 Component<br>
        🟠 Open Issue &nbsp;
        🔴 Closed Issue<br><br>
        <b>Edges (Claims):</b><br>
        <span style="color:#4A90E2">■</span> REPORTED_BY &nbsp;
        <span style="color:#F5A623">■</span> ASSIGNED_TO<br>
        <span style="color:#E74C3C">■</span> AFFECTS &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
        <span style="color:#9B59B6">■</span> DECIDED<br>
        <span style="color:#1ABC9C">■</span> RELATES_TO<br><br>
        <i style="color:#aaa">Hover nodes/edges for details<br>
        Drag to explore • Scroll to zoom</i>
    </div>
    """
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("<body>", f"<body>{legend_html}", 1)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(content)


# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — Build Evidence Panel
# ════════════════════════════════════════════════════════════════════════════

def build_evidence_panel():
    """
    Builds a searchable HTML evidence browser.
    Shows all claims with their supporting evidence excerpts.
    """
    print("\n📋 Building evidence panel...")

    conn   = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get top claims first (distinct, no join)
    cursor.execute("""
        SELECT DISTINCT c.id, c.claim_type, c.subject, c.object,
               c.confidence, c.support_count, c.seen_in_issues
        FROM claims c
        WHERE c.is_current = 1
        ORDER BY c.support_count DESC, c.confidence DESC
        LIMIT 300
    """)
    rows = cursor.fetchall()

    # Build claims dict and fetch evidence separately for each
    claims_dict = {}
    for row in rows:
        cid = row["id"]
        claims_dict[cid] = {
            "id":            cid,
            "claim_type":    row["claim_type"],
            "subject":       row["subject"],
            "object":        row["object"],
            "confidence":    row["confidence"],
            "support_count": row["support_count"],
            "evidence":      []
        }
        # Fetch evidence separately for each claim
        cursor.execute("""
            SELECT source_id, excerpt, timestamp
            FROM evidence
            WHERE claim_id = ? AND excerpt != ''
            LIMIT 3
        """, (cid,))
        for ev in cursor.fetchall():
            claims_dict[cid]["evidence"].append({
                "source_id": ev["source_id"],
                "excerpt":   ev["excerpt"],
                "timestamp": ev["timestamp"],
            })

    # Get conflicts
    cursor.execute("""
        SELECT conflict_type, issue_number, description, history, current_state
        FROM conflicts
        ORDER BY issue_number
        LIMIT 50
    """)
    conflicts = [dict(r) for r in cursor.fetchall()]
    for c in conflicts:
        c["history"] = json.loads(c["history"] or "[]")

    conn.close()

    # Build HTML
    claims_list = list(claims_dict.values())
    html = build_evidence_html(claims_list, conflicts)

    output_path = VIZ_DIR / "evidence_panel.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"  ✅ Evidence panel saved: {output_path}")


def build_evidence_html(claims: list, conflicts: list) -> str:
    """Generate the evidence panel HTML."""

    claim_type_colors = {
        "REPORTED_BY":  "#4A90E2",
        "ASSIGNED_TO":  "#F5A623",
        "AFFECTS":      "#E74C3C",
        "DECIDED":      "#9B59B6",
        "RELATES_TO":   "#1ABC9C",
        "COMMENTED_ON": "#95A5A6",
    }

    # Build claims HTML
    claims_html = ""
    for claim in claims:
        color    = claim_type_colors.get(claim["claim_type"], "#888")
        conf_pct = int(claim["confidence"] * 100)
        ev_html  = ""
        for ev in claim["evidence"][:3]:
            source_url = ""
            if "fastapi/issue/" in ev["source_id"]:
                parts = ev["source_id"].split("/")
                if len(parts) >= 3:
                    issue_num  = parts[2]
                    source_url = f"https://github.com/tiangolo/fastapi/issues/{issue_num}"

            ev_html += f"""
            <div class="evidence-item">
                <span class="excerpt">"{ev['excerpt'][:120]}"</span><br>
                <span class="source">
                    📎 {ev['source_id']}
                    {f'<a href="{source_url}" target="_blank">🔗 View on GitHub</a>' if source_url else ''}
                    &nbsp;|&nbsp; 🕐 {ev['timestamp'][:10] if ev['timestamp'] else 'unknown'}
                </span>
            </div>"""

        claims_html += f"""
        <div class="claim-card" data-type="{claim['claim_type']}">
            <div class="claim-header">
                <span class="badge" style="background:{color}">{claim['claim_type']}</span>
                <span class="claim-rel">
                    <b>{claim['subject']}</b>
                    &nbsp;→&nbsp;
                    <b>{claim['object'][:60]}</b>
                </span>
                <span class="stats">
                    🎯 {conf_pct}% confidence &nbsp;|&nbsp;
                    📚 {claim['support_count']} source(s)
                </span>
            </div>
            <div class="evidence-list">{ev_html if ev_html else '<i>No evidence excerpts</i>'}</div>
        </div>"""

    # Build conflicts HTML
    conflicts_html = ""
    for conflict in conflicts:
        history_html = ""
        for h in conflict["history"]:
            state    = h.get("state") or h.get("assigned_to") or h.get("unassigned", "")
            actor    = h.get("changed_by") or h.get("assigned_by") or h.get("by", "")
            ts       = h.get("timestamp", "")[:10]
            history_html += f"""
            <div class="history-item">
                🔄 <b>{state}</b> by {actor} on {ts}
            </div>"""

        conflicts_html += f"""
        <div class="conflict-card">
            <div class="conflict-header">
                <span class="badge" style="background:#E67E22">
                    {conflict['conflict_type'].replace('_', ' ').upper()}
                </span>
                <span> Issue #{conflict['issue_number']}</span>
                <span class="current-state">Current: {conflict['current_state'] or 'unknown'}</span>
            </div>
            <p>{conflict['description']}</p>
            <div class="history">{history_html}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Layer10 — Evidence Panel</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f0f1a;
    color: #e0e0e0;
    padding: 20px;
  }}
  h1 {{ color: #4A90E2; margin-bottom: 5px; font-size: 24px; }}
  h2 {{ color: #7ED321; margin: 25px 0 12px; font-size: 18px; }}
  .subtitle {{ color: #888; margin-bottom: 20px; font-size: 14px; }}

  /* Search + filter bar */
  .controls {{
    display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap;
  }}
  input[type=text] {{
    flex: 1; padding: 10px 15px; border-radius: 8px;
    border: 1px solid #333; background: #1a1a2e;
    color: white; font-size: 14px; min-width: 200px;
  }}
  select {{
    padding: 10px 15px; border-radius: 8px;
    border: 1px solid #333; background: #1a1a2e;
    color: white; font-size: 14px;
  }}

  /* Claim cards */
  .claim-card {{
    background: #1a1a2e; border: 1px solid #2a2a4a;
    border-radius: 10px; padding: 15px; margin-bottom: 12px;
    transition: border-color 0.2s;
  }}
  .claim-card:hover {{ border-color: #4A90E2; }}
  .claim-header {{
    display: flex; align-items: center; gap: 10px;
    flex-wrap: wrap; margin-bottom: 10px;
  }}
  .badge {{
    padding: 3px 10px; border-radius: 20px;
    font-size: 11px; font-weight: bold; color: white;
    white-space: nowrap;
  }}
  .claim-rel {{ font-size: 14px; flex: 1; }}
  .stats {{ font-size: 12px; color: #888; white-space: nowrap; }}

  /* Evidence items */
  .evidence-list {{ margin-top: 8px; }}
  .evidence-item {{
    background: #0f0f1a; border-left: 3px solid #333;
    padding: 8px 12px; margin-bottom: 6px; border-radius: 4px;
    font-size: 13px;
  }}
  .excerpt {{ color: #a0c4ff; font-style: italic; }}
  .source {{ color: #666; font-size: 11px; margin-top: 4px; }}
  .source a {{ color: #4A90E2; text-decoration: none; }}
  .source a:hover {{ text-decoration: underline; }}

  /* Conflict cards */
  .conflict-card {{
    background: #1a1a2e; border: 1px solid #3a2a1a;
    border-radius: 10px; padding: 15px; margin-bottom: 12px;
  }}
  .conflict-header {{
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 8px; flex-wrap: wrap;
  }}
  .current-state {{ font-size: 12px; color: #888; }}
  .history {{ margin-top: 8px; }}
  .history-item {{
    background: #0f0f1a; padding: 6px 10px;
    margin-bottom: 4px; border-radius: 4px; font-size: 13px;
  }}

  /* Tabs */
  .tabs {{ display: flex; gap: 5px; margin-bottom: 20px; }}
  .tab {{
    padding: 10px 20px; border-radius: 8px; cursor: pointer;
    background: #1a1a2e; border: 1px solid #333; color: #888;
    font-size: 14px; transition: all 0.2s;
  }}
  .tab.active {{ background: #4A90E2; border-color: #4A90E2; color: white; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}

  .count {{ color: #888; font-size: 14px; margin-bottom: 10px; }}
</style>
</head>
<body>

<h1>🧠 Layer10 — Evidence Panel</h1>
<p class="subtitle">
  Browse all extracted claims with supporting evidence.
  Every claim is grounded in source text from FastAPI GitHub issues.
</p>

<div class="tabs">
  <div class="tab active" onclick="showTab('claims')">
    🔗 Claims ({len(claims)})
  </div>
  <div class="tab" onclick="showTab('conflicts')">
    ⚡ Conflicts & Revisions ({len(conflicts)})
  </div>
</div>

<!-- Claims Tab -->
<div id="tab-claims" class="tab-content active">
  <div class="controls">
    <input type="text" id="search" placeholder="Search claims, persons, components..."
           onkeyup="filterClaims()">
    <select id="typeFilter" onchange="filterClaims()">
      <option value="">All claim types</option>
      <option value="REPORTED_BY">REPORTED_BY</option>
      <option value="ASSIGNED_TO">ASSIGNED_TO</option>
      <option value="AFFECTS">AFFECTS</option>
      <option value="DECIDED">DECIDED</option>
      <option value="RELATES_TO">RELATES_TO</option>
      <option value="COMMENTED_ON">COMMENTED_ON</option>
    </select>
  </div>
  <div class="count" id="claimCount">{len(claims)} claims shown</div>
  <div id="claimsList">
    {claims_html}
  </div>
</div>

<!-- Conflicts Tab -->
<div id="tab-conflicts" class="tab-content">
  <h2>⚡ Conflicts & Revisions</h2>
  <p style="color:#888; margin-bottom:15px; font-size:14px;">
    Issues where state changed multiple times — showing "used to be true vs currently true"
  </p>
  {conflicts_html}
</div>

<script>
function showTab(name) {{
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}}

function filterClaims() {{
  const search = document.getElementById('search').value.toLowerCase();
  const type   = document.getElementById('typeFilter').value;
  const cards  = document.querySelectorAll('.claim-card');
  let visible  = 0;

  cards.forEach(card => {{
    const text     = card.innerText.toLowerCase();
    const cardType = card.getAttribute('data-type');
    const matchSearch = !search || text.includes(search);
    const matchType   = !type || cardType === type;

    if (matchSearch && matchType) {{
      card.style.display = 'block';
      visible++;
    }} else {{
      card.style.display = 'none';
    }}
  }});

  document.getElementById('claimCount').textContent = visible + ' claims shown';
}}
</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Layer10 — Visualization Builder")
    print("=" * 60)

    # Build main graph
    graph_path = build_main_graph()

    # Build evidence panel
    build_evidence_panel()

    print("\n" + "=" * 60)
    print("  Visualization Complete!")
    print(f"\n  Open these files in your browser:")
    print(f"  📊 Graph:    data/visualization/graph.html")
    print(f"  📋 Evidence: data/visualization/evidence_panel.html")
    print("\n  To open:")
    print("  start data\\visualization\\graph.html")
    print("  start data\\visualization\\evidence_panel.html")
    print("=" * 60)