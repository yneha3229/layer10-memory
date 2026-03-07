"""
extract.py
----------
Phase 2 + 3: Schema Design, Structured Extraction, Validation & Repair.

Reads raw GitHub issues + comments, sends them to Groq (LLaMA),
extracts typed entities and claims with evidence, validates with Pydantic,
and saves results to data/extracted/.

Usage:
    python extract.py

Requirements:
    pip install groq pydantic python-dotenv
"""

import os
import json
import time
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from groq import Groq
from pydantic import BaseModel, ValidationError, field_validator

# ── Load environment ────────────────────────────────────────────────────────
load_dotenv()

API_KEYS = [
    os.getenv("GROQ_API_KEY"),
    os.getenv("GROQ_API_KEY_2"),
]
API_KEYS = [k for k in API_KEYS if k]  # remove any missing keys
current_key_index = 0
client = Groq(api_key=API_KEYS[0])
# ── Directories ─────────────────────────────────────────────────────────────
DATA_DIR      = Path("data")
ISSUES_DIR    = DATA_DIR / "issues"
COMMENTS_DIR  = DATA_DIR / "comments"
EXTRACTED_DIR = DATA_DIR / "extracted"
FAILED_DIR    = DATA_DIR / "failed"

EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
FAILED_DIR.mkdir(parents=True, exist_ok=True)

# ── How many issues to process (start small, increase later) ────────────────
MAX_ISSUES = 500  # Process first 100 issues. Raise to 500+ later.

# ── Pydantic Schema (Our Ontology) ──────────────────────────────────────────

class Evidence(BaseModel):
    """
    Every claim must have evidence pointing back to the exact source.
    This is the core grounding requirement.
    """
    source_id: str          # e.g. "fastapi/issue/123/comment/456"
    excerpt: str            # exact text snippet from the source
    timestamp: str          # ISO timestamp of the source

class Person(BaseModel):
    """A GitHub user mentioned in the issue."""
    entity_type: str = "Person"
    username: str           # GitHub username (canonical identifier)
    display_name: Optional[str] = None

class Component(BaseModel):
    """A software component/area affected by the issue (e.g. routing, auth, docs)."""
    entity_type: str = "Component"
    name: str               # normalized component name (lowercase)

class Decision(BaseModel):
    """
    A decision made in the issue discussion.
    e.g. 'decided to deprecate X', 'agreed to close as wontfix'
    """
    entity_type: str = "Decision"
    summary: str            # one sentence summary of the decision
    decision_type: str      # one of: accepted / rejected / deferred / reversed
    evidence: Evidence      # must point to exact comment where decision was made

class Claim(BaseModel):
    """
    A typed relationship/fact extracted from the issue.
    e.g. Person X reported Issue Y, Issue affects Component Z
    """
    claim_type: str         # REPORTED_BY / ASSIGNED_TO / AFFECTS / DECIDED / RELATES_TO
    subject: str            # who/what the claim is about (username or issue number)
    object: str             # the target (component name, decision summary, issue number)
    confidence: float       # 0.0 to 1.0 — how confident the model is
    evidence: Evidence      # grounding — exact source for this claim

    @field_validator("confidence")
    @classmethod
    def confidence_must_be_valid(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return round(v, 2)

    @field_validator("claim_type")
    @classmethod
    def claim_type_must_be_valid(cls, v):
        valid = {"REPORTED_BY", "ASSIGNED_TO", "AFFECTS", "DECIDED", "RELATES_TO", "COMMENTED_ON"}
        if v not in valid:
            raise ValueError(f"claim_type must be one of {valid}")
        return v

class ExtractionResult(BaseModel):
    """
    Full extraction output for one issue.
    This is what gets saved to data/extracted/issue_XXX.json
    """
    issue_number: int
    issue_title: str
    issue_state: str                    # open or closed
    issue_url: str
    extracted_at: str                   # when we ran extraction
    extraction_version: str = "v1.0"   # track schema/prompt version
    persons: list[Person]
    components: list[Component]
    decisions: list[Decision]
    claims: list[Claim]
    high_confidence_claims: list[Claim] = []  # claims with confidence >= 0.7


# ── Extraction Prompt ────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """You are an expert at extracting structured information from GitHub issues.

Given a GitHub issue and its comments, extract the following:

1. PERSONS: All GitHub users mentioned (reporters, assignees, commenters)
2. COMPONENTS: Software components/areas affected (e.g. "routing", "authentication", "middleware", "docs", "dependencies")
3. DECISIONS: Any decisions made in the discussion (e.g. "decided to close as wontfix", "agreed to implement in v2")
4. CLAIMS: Typed relationships between entities

For CLAIMS, use these types only:
- REPORTED_BY: who filed the issue
- ASSIGNED_TO: who was assigned
- AFFECTS: what component is affected
- DECIDED: what decision was reached
- RELATES_TO: if issue mentions another issue
- COMMENTED_ON: who actively participated

CRITICAL RULES:
- Every decision and claim MUST have evidence with an exact excerpt from the text
- Keep excerpts SHORT (max 100 characters)
- confidence is 0.0 to 1.0 (be honest - use 0.5 for uncertain, 0.9 for obvious facts)
- component names must be lowercase single words or short phrases
- decision_type must be one of: accepted / rejected / deferred / reversed

Respond ONLY with valid JSON. No explanation, no markdown, no backticks.
Use exactly this structure:

{
  "persons": [
    {"entity_type": "Person", "username": "githubuser", "display_name": "Display Name or null"}
  ],
  "components": [
    {"entity_type": "Component", "name": "routing"}
  ],
  "decisions": [
    {
      "entity_type": "Decision",
      "summary": "one sentence summary",
      "decision_type": "accepted",
      "evidence": {
        "source_id": "fastapi/issue/NUMBER/body",
        "excerpt": "exact short quote from text",
        "timestamp": "2024-01-01T00:00:00Z"
      }
    }
  ],
  "claims": [
    {
      "claim_type": "REPORTED_BY",
      "subject": "issue/NUMBER",
      "object": "githubuser",
      "confidence": 0.95,
      "evidence": {
        "source_id": "fastapi/issue/NUMBER/body",
        "excerpt": "exact short quote",
        "timestamp": "2024-01-01T00:00:00Z"
      }
    }
  ]
}"""


# ── Helper: Load issue + comments ───────────────────────────────────────────

def load_issue_with_comments(issue_number: int) -> dict | None:
    """Load issue JSON and its comments, combine into one dict."""
    issue_path = ISSUES_DIR / f"issue_{issue_number}.json"
    if not issue_path.exists():
        return None

    with open(issue_path, encoding="utf-8") as f:
        issue = json.load(f)

    comments = []
    comments_path = COMMENTS_DIR / f"comments_{issue_number}.json"
    if comments_path.exists():
        with open(comments_path, encoding="utf-8") as f:
            comments = json.load(f)

    return {"issue": issue, "comments": comments}


# ── Helper: Build text for LLM ──────────────────────────────────────────────

def build_issue_text(data: dict) -> str:
    """
    Convert issue + comments into a clean text block for the LLM.
    We truncate to avoid token limits.
    """
    issue = data["issue"]
    comments = data["comments"]

    lines = [
        f"ISSUE #{issue['number']}: {issue['title']}",
        f"State: {issue['state']}",
        f"Author: {issue['user']['login']}",
        f"Created: {issue['created_at']}",
        f"Labels: {', '.join(l['name'] for l in issue.get('labels', []))}",
        f"Assignees: {', '.join(a['login'] for a in issue.get('assignees', []))}",
        "",
        "BODY:",
        (issue.get("body") or "")[:1000],  # truncate long bodies
        "",
        "COMMENTS:",
    ]

    # Include up to 5 comments to stay within token limits
    for i, comment in enumerate(comments[:5]):
        lines.append(f"[Comment by {comment['user']['login']} at {comment['created_at']}]")
        lines.append((comment.get("body") or "")[:500])
        lines.append("")

    return "\n".join(lines)


# ── Helper: Call Groq with retries ──────────────────────────────────────────

def call_groq(issue_text: str, issue_number: int) -> str | None:
    global current_key_index, client

    for attempt in range(6):  # 3 attempts per key x 2 keys
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                max_tokens=2000,
                temperature=0.1,
                messages=[
                    {"role": "system", "content": EXTRACTION_PROMPT},
                    {"role": "user", "content": f"Extract from this GitHub issue:\n\n{issue_text}"}
                ]
            )
            return response.choices[0].message.content

        except Exception as e:
            error_str = str(e)
            # If rate limited -> switch to next API key
            if "429" in error_str and current_key_index < len(API_KEYS) - 1:
                current_key_index += 1
                client = Groq(api_key=API_KEYS[current_key_index])
                print(f"    🔄 Switched to API key #{current_key_index + 1}")
                continue
            else:
                print(f"    ⚠️  Groq error attempt {attempt+1}: {e}")
                time.sleep(3)

    return None


# ── Helper: Parse + validate LLM output ─────────────────────────────────────

def parse_and_validate(raw: str, issue: dict) -> ExtractionResult | None:
    """
    Parse the LLM JSON output and validate with Pydantic.
    Handles common repair cases.
    Returns ExtractionResult or None if unrecoverable.
    """
    # Step 1: Clean up common LLM formatting mistakes
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # Remove markdown code fences if model added them
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    # Step 2: Parse JSON
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"    ❌ JSON parse error: {e}")
        return None

    # Step 3: Build ExtractionResult with Pydantic validation
    try:
        issue_number = issue["number"]
        result = ExtractionResult(
            issue_number=issue_number,
            issue_title=issue["title"],
            issue_state=issue["state"],
            issue_url=issue["html_url"],
            extracted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            persons=data.get("persons", []),
            components=data.get("components", []),
            decisions=data.get("decisions", []),
            claims=data.get("claims", []),
        )

        # Step 4: Filter high confidence claims (quality gate)
        result.high_confidence_claims = [
            c for c in result.claims if c.confidence >= 0.7
        ]

        return result

    except ValidationError as e:
        print(f"    ❌ Validation error: {e.error_count()} errors")
        # Try partial recovery — remove invalid claims and retry
        try:
            valid_claims = []
            for claim in data.get("claims", []):
                try:
                    valid_claims.append(Claim(**claim))
                except ValidationError:
                    pass  # skip invalid claims

            valid_decisions = []
            for decision in data.get("decisions", []):
                try:
                    valid_decisions.append(Decision(**decision))
                except ValidationError:
                    pass

            issue_number = issue["number"]
            result = ExtractionResult(
                issue_number=issue_number,
                issue_title=issue["title"],
                issue_state=issue["state"],
                issue_url=issue["html_url"],
                extracted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                persons=data.get("persons", []),
                components=data.get("components", []),
                decisions=valid_decisions,
                claims=valid_claims,
            )
            result.high_confidence_claims = [
                c for c in result.claims if c.confidence >= 0.7
            ]
            print(f"    ⚠️  Partial recovery: kept {len(valid_claims)} claims")
            return result

        except Exception:
            return None


# ── Main extraction loop ─────────────────────────────────────────────────────

def run_extraction():
    """
    Main loop: processes each issue file, extracts structured data,
    validates, and saves results.
    """
    # Get all issue files sorted by issue number
    issue_files = sorted(ISSUES_DIR.glob("issue_*.json"),
                         key=lambda p: int(p.stem.split("_")[1]))

    total     = min(len(issue_files), MAX_ISSUES)
    success   = 0
    failed    = 0
    skipped   = 0

    print("=" * 60)
    print("  Layer10 — Extraction Pipeline")
    print(f"  Processing {total} issues")
    print("=" * 60)

    for idx, issue_file in enumerate(issue_files[:MAX_ISSUES]):
        issue_number = int(issue_file.stem.split("_")[1])
        out_path     = EXTRACTED_DIR / f"extracted_{issue_number}.json"

        # Skip if already extracted
        if out_path.exists():
            skipped += 1
            continue

        print(f"\n[{idx+1}/{total}] Issue #{issue_number} ...", end=" ")

        # Load issue + comments
        data = load_issue_with_comments(issue_number)
        if not data:
            print("❌ Could not load")
            failed += 1
            continue

        # Build text for LLM
        issue_text = build_issue_text(data)

        # Call Groq
        raw_response = call_groq(issue_text, issue_number)
        if not raw_response:
            print("❌ Groq failed")
            failed += 1
            # Save to failed folder for review
            (FAILED_DIR / f"failed_{issue_number}.txt").write_text(
                "Groq API failed after 3 retries", encoding="utf-8"
            )
            continue

        # Parse + validate
        result = parse_and_validate(raw_response, data["issue"])
        if not result:
            print("❌ Parse/validation failed")
            failed += 1
            # Save raw response for debugging
            (FAILED_DIR / f"failed_{issue_number}.txt").write_text(
                raw_response, encoding="utf-8"
            )
            continue

        # Save result
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result.model_dump(), f, indent=2, ensure_ascii=False)

        success += 1
        print(f"✅ {len(result.persons)} persons, "
              f"{len(result.components)} components, "
              f"{len(result.decisions)} decisions, "
              f"{len(result.claims)} claims "
              f"({len(result.high_confidence_claims)} high-conf)")

        # Small delay to respect Groq rate limits
        time.sleep(0.5)

    # Final summary
    print("\n" + "=" * 60)
    print(f"  Extraction Complete!")
    print(f"  ✅ Success:  {success}")
    print(f"  ⏭️  Skipped:  {skipped} (already extracted)")
    print(f"  ❌ Failed:   {failed}")
    print(f"  📁 Results saved to: data/extracted/")
    print("=" * 60)


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_extraction()