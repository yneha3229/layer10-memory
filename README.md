#  Grounded Long-Term Memory

A system that turns scattered GitHub issue discussions into a grounded long-term memory graph — with structured extraction, deduplication, conflict tracking, and interactive visualization.

**Corpus:** FastAPI GitHub Issues (`tiangolo/fastapi`) — 500 issues, 495 extracted  
**Model:** LLaMA 3.1 8B Instant via Groq API (free tier)  
**Stack:** Python, SQLite, NetworkX, PyVis, Pydantic, RapidFuzz

---

## Project Stats

| Metric | Value |
|--------|-------|
| Issues Collected | 500 |
| Issues Extracted | 495 |
| Raw Claims Extracted | 2,832 |
| Unique Claims (after dedup) | 932 |
| Unique Persons | 547 |
| Unique Components | 594 |
| Merge Operations Logged | 2,008 |
| Conflicts/Revisions Found | 409 |
| Graph Nodes | 1,636 |
| Graph Edges | 698 |

---

## Quick Start

### 1. Clone and install
```bash
git clone https://github.com/yneha3229/layer10-memory.git
cd layer10-memory
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure API keys
```bash
cp .env.example .env
# Edit .env and add your keys
```

Get a free GitHub token at: https://github.com/settings/tokens  
Get a free Groq API key at: https://console.groq.com

### 3. Run the full pipeline
```bash
# Step 1: Download data from GitHub
python fetch_data.py

# Step 2: Extract entities and claims using LLaMA
python extract.py

# Step 3: Deduplicate and canonicalize
python dedup.py

# Step 4: Build the memory graph
python build_graph.py

# Step 5: Run retrieval examples
python retrieval.py

# Step 6: Build visualizations
python visualize.py
```

### 4. Open visualizations
```bash
# Windows
start data\visualization\graph.html
start data\visualization\evidence_panel.html

# Mac/Linux
open data/visualization/graph.html
open data/visualization/evidence_panel.html
```

---

## Pipeline Overview

```
GitHub API
    ↓
fetch_data.py      → data/issues/, data/comments/, data/events/
    ↓
extract.py         → data/extracted/  (Groq LLaMA + Pydantic validation)
    ↓
dedup.py           → data/deduped/    (entity canon + claim dedup + merge log)
    ↓
build_graph.py     → data/graph/      (SQLite DB + NetworkX graph JSON)
    ↓
retrieval.py       → data/retrieval/  (5 grounded context packs)
    ↓
visualize.py       → data/visualization/ (interactive HTML graph + evidence panel)
```

---

## Output Files

| File | Description |
|------|-------------|
| `data/graph/memory.db` | SQLite database — queryable memory store |
| `data/graph/graph.json` | Serialized graph (nodes + edges) |
| `data/deduped/canonical_entities.json` | 547 unique persons, 594 unique components |
| `data/deduped/canonical_claims.json` | 932 unique claims with evidence |
| `data/deduped/merge_log.json` | 2,008 merge records (reversible audit trail) |
| `data/deduped/conflicts.json` | 409 conflicts/revisions |
| `data/retrieval/context_pack_*.json` | Example grounded context packs |
| `data/visualization/graph.html` | Interactive memory graph (open in browser) |
| `data/visualization/evidence_panel.html` | Searchable evidence browser (open in browser) |

---

## Schema / Ontology

### Entity Types
- **Person** — GitHub user (canonical: username)
- **Component** — Software area (e.g., routing, dependencies, middleware)
- **Issue** — GitHub issue artifact
- **Decision** — A conclusion reached in discussion

### Claim Types
| Type | Meaning |
|------|---------|
| REPORTED_BY | Who filed the issue |
| ASSIGNED_TO | Who was assigned |
| AFFECTS | What component is affected |
| DECIDED | What decision was reached |
| RELATES_TO | Links to another issue/resource |
| COMMENTED_ON | Who actively participated |

### Evidence Structure
Every claim carries:
```json
{
  "source_id": "fastapi/issue/123/comment/456",
  "excerpt": "exact text snippet from source",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

---

## Example Retrieval Questions

```bash
python retrieval.py
```

Answers these questions with grounded evidence:
1. "Who reported the most issues?"
2. "What components have the most bugs?"
3. "Which issues were reopened after being closed?"
4. "What decisions were made about dependencies?"
5. "Who is assigned to routing issues?"

---

## Requirements

- Python 3.11+
- GitHub personal access token (free, public repos only)
- Groq API key (free tier — 500k tokens/day)

---

## Notes on Groq Rate Limits

The free Groq tier allows ~500,000 tokens/day (~357 issues). If you hit the limit:
- The script saves all completed extractions automatically
- Re-run `python extract.py` the next day — it skips already-extracted issues
- Alternatively, add a second Groq API key as `GROQ_API_KEY_2` in `.env`
