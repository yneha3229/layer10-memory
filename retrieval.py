"""
retrieval.py
------------
Phase 6: Retrieval API

Given a natural language question, searches the memory graph
and returns a grounded context pack with ranked evidence.

Usage:
    python retrieval.py

Output:
    data/retrieval/context_pack_N.json  - example context packs
"""

import json
import sqlite3
import re
from pathlib import Path

# ── Directories ──────────────────────────────────────────────────────────────
DATA_DIR     = Path("data")
GRAPH_DIR    = DATA_DIR / "graph"
RETRIEVAL    = DATA_DIR / "retrieval"
RETRIEVAL.mkdir(parents=True, exist_ok=True)

DB_PATH      = GRAPH_DIR / "memory.db"


# ════════════════════════════════════════════════════════════════════════════
# CORE RETRIEVAL ENGINE
# ════════════════════════════════════════════════════════════════════════════

class MemoryRetriever:
    """
    Retrieves grounded context from the memory graph.
    Given a question, returns a ranked list of claims + evidence.
    """

    def __init__(self):
        self.conn   = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row  # access columns by name
        print("✅ Connected to memory graph")

    def retrieve(self, question: str, top_k: int = 5) -> dict:
        """
        Main retrieval method.
        1. Extract keywords from question
        2. Search claims via full text search
        3. Search entities by name
        4. Rank results by confidence + support
        5. Fetch evidence for each result
        6. Return grounded context pack
        """
        print(f"\n🔍 Question: {question}")

        # Step 1: Extract keywords
        keywords = self._extract_keywords(question)
        print(f"   Keywords: {keywords}")

        # Step 2: Search claims (full text search)
        claim_results = self._search_claims(keywords)

        # Step 3: Search entities
        entity_results = self._search_entities(keywords)

        # Step 4: Search issues by title
        issue_results = self._search_issues(keywords)

        # Step 5: Rank all results
        ranked = self._rank_results(claim_results, question)

        # Step 6: Fetch evidence for top results
        context_items = []
        for claim in ranked[:top_k]:
            evidence = self._fetch_evidence(claim["id"])
            context_items.append({
                "claim_type":    claim["claim_type"],
                "subject":       claim["subject"],
                "object":        claim["object"],
                "confidence":    claim["confidence"],
                "support_count": claim["support_count"],
                "evidence":      evidence,
                "citation":      self._format_citation(claim, evidence),
            })

        # Build context pack
        context_pack = {
            "question":      question,
            "keywords":      keywords,
            "results":       context_items,
            "entities_found": entity_results[:5],
            "issues_found":  issue_results[:5],
            "total_results": len(ranked),
            "grounded":      all(len(c["evidence"]) > 0 for c in context_items),
        }

        self._print_results(context_pack)
        return context_pack

    # ── Keyword extraction ───────────────────────────────────────────────────

    def _extract_keywords(self, question: str) -> list[str]:
        """
        Extract meaningful keywords from a question.
        Removes stopwords, keeps nouns/verbs likely to match claims.
        """
        stopwords = {
            "who", "what", "when", "where", "how", "why", "is", "are",
            "was", "were", "the", "a", "an", "in", "on", "at", "to",
            "for", "of", "and", "or", "but", "with", "about", "did",
            "do", "does", "has", "have", "had", "been", "be", "most",
            "many", "any", "all", "which", "that", "this", "these"
        }
        words = re.findall(r'\b[a-zA-Z]{3,}\b', question.lower())
        return [w for w in words if w not in stopwords]

    # ── Full text search on claims ───────────────────────────────────────────

    def _search_claims(self, keywords: list[str]) -> list[dict]:
        """Search claims using SQLite FTS and keyword matching."""
        if not keywords:
            return []

        cursor = self.conn.cursor()
        results = []
        seen_ids = set()

        # FTS search for each keyword
        for keyword in keywords:
            try:
                cursor.execute("""
                    SELECT c.id, c.claim_type, c.subject, c.object,
                           c.confidence, c.support_count, c.seen_in_issues
                    FROM claims c
                    JOIN claims_fts f ON c.id = f.claim_id
                    WHERE claims_fts MATCH ?
                    AND c.is_current = 1
                    ORDER BY c.confidence DESC, c.support_count DESC
                    LIMIT 20
                """, (keyword,))

                for row in cursor.fetchall():
                    if row["id"] not in seen_ids:
                        results.append(dict(row))
                        seen_ids.add(row["id"])
            except Exception:
                # Fallback to LIKE search if FTS fails
                cursor.execute("""
                    SELECT id, claim_type, subject, object,
                           confidence, support_count, seen_in_issues
                    FROM claims
                    WHERE (subject LIKE ? OR object LIKE ?)
                    AND is_current = 1
                    ORDER BY confidence DESC, support_count DESC
                    LIMIT 20
                """, (f"%{keyword}%", f"%{keyword}%"))

                for row in cursor.fetchall():
                    if row["id"] not in seen_ids:
                        results.append(dict(row))
                        seen_ids.add(row["id"])

        return results

    # ── Entity search ────────────────────────────────────────────────────────

    def _search_entities(self, keywords: list[str]) -> list[dict]:
        """Search entities by canonical name and aliases."""
        if not keywords:
            return []

        cursor = self.conn.cursor()
        results = []

        for keyword in keywords:
            cursor.execute("""
                SELECT id, entity_type, canonical_name,
                       mention_count, aliases
                FROM entities
                WHERE canonical_name LIKE ?
                OR aliases LIKE ?
                ORDER BY mention_count DESC
                LIMIT 5
            """, (f"%{keyword}%", f"%{keyword}%"))

            for row in cursor.fetchall():
                results.append({
                    "id":            row["id"],
                    "type":          row["entity_type"],
                    "name":          row["canonical_name"],
                    "mention_count": row["mention_count"],
                })

        return results

    # ── Issue search ─────────────────────────────────────────────────────────

    def _search_issues(self, keywords: list[str]) -> list[dict]:
        """Search issues by title."""
        if not keywords:
            return []

        cursor = self.conn.cursor()
        results = []

        for keyword in keywords:
            cursor.execute("""
                SELECT id, title, state, url
                FROM issues
                WHERE title LIKE ?
                ORDER BY id DESC
                LIMIT 5
            """, (f"%{keyword}%",))

            for row in cursor.fetchall():
                results.append({
                    "id":    row["id"],
                    "title": row["title"],
                    "state": row["state"],
                    "url":   row["url"],
                })

        return results

    # ── Ranking ──────────────────────────────────────────────────────────────

    def _rank_results(self, claims: list[dict], question: str) -> list[dict]:
        """
        Ranks claims by:
        1. Keyword overlap with question (relevance)
        2. Support count (how many evidence sources back it up)
        3. Confidence score
        """
        question_words = set(question.lower().split())

        for claim in claims:
            # Keyword overlap score
            claim_words  = set(claim["subject"].split()) | set(claim["object"].split())
            overlap      = len(question_words & claim_words)

            # Combined score
            claim["score"] = (
                overlap * 2.0 +
                claim["support_count"] * 1.5 +
                claim["confidence"] * 1.0
            )

        return sorted(claims, key=lambda x: x.get("score", 0), reverse=True)

    # ── Evidence fetching ────────────────────────────────────────────────────

    def _fetch_evidence(self, claim_id: str) -> list[dict]:
        """Fetch all evidence for a claim from the evidence table."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT source_id, excerpt, timestamp
            FROM evidence
            WHERE claim_id = ?
            ORDER BY timestamp ASC
            LIMIT 3
        """, (claim_id,))

        return [
            {
                "source_id": row["source_id"],
                "excerpt":   row["excerpt"],
                "timestamp": row["timestamp"],
            }
            for row in cursor.fetchall()
        ]

    # ── Citation formatting ──────────────────────────────────────────────────

    def _format_citation(self, claim: dict, evidence: list[dict]) -> str:
        """
        Formats a human-readable citation for a claim.
        Example: [REPORTED_BY] issue/123 → tiangolo
                 Source: fastapi/issue/123/body (2024-01-15)
        """
        lines = [
            f"[{claim['claim_type']}] {claim['subject']} → {claim['object']}",
            f"Confidence: {claim['confidence']:.0%} | "
            f"Supported by {claim['support_count']} source(s)",
        ]
        for i, ev in enumerate(evidence[:2]):
            lines.append(f"  Evidence {i+1}: \"{ev['excerpt'][:80]}\"")
            lines.append(f"            Source: {ev['source_id']} ({ev['timestamp'][:10]})")
        return "\n".join(lines)

    # ── Pretty print ─────────────────────────────────────────────────────────

    def _print_results(self, pack: dict):
        """Print a readable summary of the context pack."""
        print(f"\n{'='*60}")
        print(f"📦 CONTEXT PACK — {pack['total_results']} results found")
        print(f"   Grounded: {'✅ Yes' if pack['grounded'] else '⚠️  Some missing evidence'}")

        if pack["entities_found"]:
            print(f"\n🏷️  Entities matched:")
            for e in pack["entities_found"]:
                print(f"   • {e['type']}: {e['name']} (mentioned {e['mention_count']}x)")

        if pack["issues_found"]:
            print(f"\n📋 Issues matched:")
            for i in pack["issues_found"]:
                print(f"   • #{i['id']}: {i['title'][:60]} [{i['state']}]")

        print(f"\n🔗 Top claims:")
        for idx, item in enumerate(pack["results"]):
            print(f"\n  [{idx+1}] {item['citation']}")

        print(f"{'='*60}")

    def close(self):
        self.conn.close()


# ════════════════════════════════════════════════════════════════════════════
# EXAMPLE QUESTIONS
# ════════════════════════════════════════════════════════════════════════════

EXAMPLE_QUESTIONS = [
    "Who reported the most issues?",
    "What components have the most bugs?",
    "Which issues were reopened after being closed?",
    "What decisions were made about dependencies?",
    "Who is assigned to routing issues?",
]


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Layer10 — Retrieval API")
    print("=" * 60)

    retriever = MemoryRetriever()

    # Run all example questions and save context packs
    all_packs = []
    for idx, question in enumerate(EXAMPLE_QUESTIONS):
        pack = retriever.retrieve(question, top_k=5)
        all_packs.append(pack)

        # Save individual context pack
        out_path = RETRIEVAL / f"context_pack_{idx+1}.json"
        out_path.write_text(
            json.dumps(pack, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        print(f"\n💾 Saved: {out_path.name}")

    # Save all packs together
    (RETRIEVAL / "all_context_packs.json").write_text(
        json.dumps(all_packs, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    retriever.close()

    print("\n" + "=" * 60)
    print("  Retrieval Complete!")
    print(f"  📁 Context packs saved to: data/retrieval/")
    print("  Next step: python visualize.py")
    print("=" * 60)