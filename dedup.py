"""
dedup.py
--------
Phase 4: Deduplication & Canonicalization

Reads all extracted JSON files and:
1. Deduplicates persons (same user mentioned 100 different ways)
2. Canonicalizes components (routing/router/routes -> routing)
3. Deduplicates claims (same fact stated multiple times)
4. Tracks conflicts and revisions (open->closed->reopened)
5. Logs all merges for reversibility/auditing

Usage:
    python dedup.py

Output:
    data/deduped/canonical_entities.json  - unique persons + components
    data/deduped/canonical_claims.json    - deduplicated claims
    data/deduped/merge_log.json           - audit trail of all merges
    data/deduped/conflicts.json           - conflicting/revised facts
"""

import json
import time
from pathlib import Path
from rapidfuzz import fuzz

# ── Directories ─────────────────────────────────────────────────────────────
DATA_DIR   = Path("data")
EXTRACTED  = DATA_DIR / "extracted"
DEDUPED    = DATA_DIR / "deduped"
DEDUPED.mkdir(parents=True, exist_ok=True)

# ── Thresholds ───────────────────────────────────────────────────────────────
PERSON_SIMILARITY    = 90   # fuzzy match threshold for person names (0-100)
COMPONENT_SIMILARITY = 85   # fuzzy match threshold for component names
CLAIM_SIMILARITY     = 80   # fuzzy match threshold for duplicate claims


# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — Load all extracted data
# ════════════════════════════════════════════════════════════════════════════

def load_all_extractions() -> list[dict]:
    """Load all extracted JSON files into memory."""
    files = sorted(EXTRACTED.glob("*.json"),
                   key=lambda p: int(p.stem.split("_")[1]))
    data = []
    for f in files:
        try:
            data.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"  ⚠️  Could not load {f.name}: {e}")
    print(f"  Loaded {len(data)} extracted issues")
    return data


# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — Person Deduplication
# ════════════════════════════════════════════════════════════════════════════

def dedup_persons(all_data: list[dict], merge_log: list) -> dict:
    """
    Deduplicates persons across all issues.

    Strategy:
    - GitHub usernames are the canonical identifier (exact match)
    - Display names get fuzzy matched to catch variations
    - Returns a dict of canonical_username -> person record with all aliases

    Example:
        "tiangolo", "Sebastián Ramírez", "tiangolo[bot]" -> canonical: "tiangolo"
    """
    print("\n👤 Deduplicating persons...")

    # canonical_id -> {username, display_names, issue_numbers, aliases}
    canonical_persons = {}

    for issue_data in all_data:
        issue_num = issue_data["issue_number"]

        for person in issue_data.get("persons", []):
            username = person.get("username", "").strip().lower()
            display  = person.get("display_name") or ""

            if not username:
                continue

            # Remove bot accounts from canonical persons
            if username.endswith("[bot]") or username.endswith("-bot"):
                continue

            # Check if we already have this username (exact match)
            if username in canonical_persons:
                # Just add this issue to the existing record
                canonical_persons[username]["seen_in_issues"].add(issue_num)
                if display and display not in canonical_persons[username]["aliases"]:
                    canonical_persons[username]["aliases"].append(display)
            else:
                # Check fuzzy match against existing usernames
                matched = False
                for existing_username in canonical_persons:
                    score = fuzz.ratio(username, existing_username)
                    if score >= PERSON_SIMILARITY:
                        # Merge into existing
                        canonical_persons[existing_username]["seen_in_issues"].add(issue_num)
                        if username not in canonical_persons[existing_username]["aliases"]:
                            canonical_persons[existing_username]["aliases"].append(username)

                        merge_log.append({
                            "type":        "person_merge",
                            "merged_from": username,
                            "merged_into": existing_username,
                            "reason":      f"fuzzy match score {score}",
                            "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "reversible":  True
                        })
                        matched = True
                        break

                if not matched:
                    # New canonical person
                    canonical_persons[username] = {
                        "entity_type":     "Person",
                        "canonical_id":    username,
                        "username":        username,
                        "aliases":         [display] if display else [],
                        "seen_in_issues":  {issue_num},
                        "mention_count":   0
                    }

    # Count mentions and convert sets to lists for JSON serialization
    for username, person in canonical_persons.items():
        person["mention_count"]  = len(person["seen_in_issues"])
        person["seen_in_issues"] = sorted(list(person["seen_in_issues"]))
        # Clean empty aliases
        person["aliases"] = [a for a in person["aliases"] if a and a != username]

    print(f"  ✅ {sum(len(d.get('persons',[])) for d in all_data)} mentions → "
          f"{len(canonical_persons)} unique persons")
    return canonical_persons


# ════════════════════════════════════════════════════════════════════════════
# STEP 3 — Component Canonicalization
# ════════════════════════════════════════════════════════════════════════════

def canonicalize_components(all_data: list[dict], merge_log: list) -> dict:
    """
    Canonicalizes component names across all issues.

    Strategy:
    - Normalize to lowercase, strip whitespace
    - Fuzzy match similar names (routing/router/routes -> routing)
    - Keep the most frequently seen version as canonical name

    Example:
        "routing", "router", "routes", "route handling" -> canonical: "routing"
    """
    print("\n🔧 Canonicalizing components...")

    # First pass: count all component mentions
    component_counts = {}
    component_issues = {}

    for issue_data in all_data:
        issue_num = issue_data["issue_number"]
        for comp in issue_data.get("components", []):
            name = comp.get("name", "").strip().lower()
            if not name:
                continue
            component_counts[name] = component_counts.get(name, 0) + 1
            if name not in component_issues:
                component_issues[name] = set()
            component_issues[name].add(issue_num)

    # Sort by frequency (most common first becomes canonical)
    sorted_components = sorted(component_counts.items(),
                               key=lambda x: x[1], reverse=True)

    # Second pass: fuzzy merge similar components
    canonical_components = {}  # canonical_name -> record
    name_to_canonical   = {}   # any_name -> canonical_name

    for name, count in sorted_components:
        # Check if already mapped
        if name in name_to_canonical:
            continue

        # Check fuzzy match against existing canonicals
        matched = False
        for canonical_name in canonical_components:
            score = fuzz.ratio(name, canonical_name)
            if score >= COMPONENT_SIMILARITY:
                # Merge into existing canonical
                canonical_components[canonical_name]["aliases"].append(name)
                canonical_components[canonical_name]["mention_count"] += count
                for issue in component_issues.get(name, set()):
                    canonical_components[canonical_name]["seen_in_issues"].add(issue)
                name_to_canonical[name] = canonical_name

                merge_log.append({
                    "type":        "component_merge",
                    "merged_from": name,
                    "merged_into": canonical_name,
                    "reason":      f"fuzzy match score {score}",
                    "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "reversible":  True
                })
                matched = True
                break

        if not matched:
            # New canonical component
            canonical_components[name] = {
                "entity_type":    "Component",
                "canonical_id":   name,
                "canonical_name": name,
                "aliases":        [],
                "mention_count":  count,
                "seen_in_issues": component_issues.get(name, set()),
            }
            name_to_canonical[name] = name

    # Convert sets to lists
    for comp in canonical_components.values():
        comp["seen_in_issues"] = sorted(list(comp["seen_in_issues"]))

    print(f"  ✅ {len(component_counts)} mentions → "
          f"{len(canonical_components)} unique components")
    return canonical_components, name_to_canonical


# ════════════════════════════════════════════════════════════════════════════
# STEP 4 — Claim Deduplication
# ════════════════════════════════════════════════════════════════════════════

def dedup_claims(all_data: list[dict],
                 name_to_canonical: dict,
                 merge_log: list) -> list:
    """
    Deduplicates claims across all issues.

    Strategy:
    - Build a claim fingerprint: claim_type + subject + object (normalized)
    - If two claims have same fingerprint -> merge evidence lists
    - If similar but not identical -> fuzzy match on subject+object
    - Keep ALL evidence pointers (never throw away provenance)
    """
    print("\n🔗 Deduplicating claims...")

    # fingerprint -> canonical claim record
    canonical_claims = {}

    for issue_data in all_data:
        issue_num = issue_data["issue_number"]

        for claim in issue_data.get("claims", []):
            claim_type = claim.get("claim_type", "").upper()
            subject    = claim.get("subject", "").strip().lower()
            obj        = claim.get("object", "").strip().lower()
            confidence = claim.get("confidence", 0.5)
            evidence   = claim.get("evidence", {})

            if not claim_type or not subject or not obj:
                continue

            # Normalize object to canonical component name if applicable
            if obj in name_to_canonical:
                obj = name_to_canonical[obj]

            # Build fingerprint
            fingerprint = f"{claim_type}|{subject}|{obj}"

            if fingerprint in canonical_claims:
                # Exact duplicate — just add evidence
                existing = canonical_claims[fingerprint]
                existing["evidence_list"].append(evidence)
                existing["support_count"] += 1
                # Update confidence to max seen
                existing["confidence"] = max(existing["confidence"], confidence)
                existing["seen_in_issues"].add(issue_num)

                merge_log.append({
                    "type":        "claim_merge",
                    "fingerprint": fingerprint,
                    "reason":      "exact duplicate fingerprint",
                    "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "reversible":  True
                })

            else:
                # Check fuzzy match against existing claims of same type
                matched = False
                for fp, existing in canonical_claims.items():
                    if not fp.startswith(claim_type):
                        continue
                    # Fuzzy match on subject+object combined
                    existing_subj_obj = fp.split("|")[1] + " " + fp.split("|")[2]
                    new_subj_obj      = subject + " " + obj
                    score = fuzz.ratio(existing_subj_obj, new_subj_obj)

                    if score >= CLAIM_SIMILARITY:
                        existing["evidence_list"].append(evidence)
                        existing["support_count"] += 1
                        existing["confidence"] = max(existing["confidence"], confidence)
                        existing["seen_in_issues"].add(issue_num)

                        merge_log.append({
                            "type":        "claim_merge",
                            "merged_from": fingerprint,
                            "merged_into": fp,
                            "reason":      f"fuzzy match score {score}",
                            "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "reversible":  True
                        })
                        matched = True
                        break

                if not matched:
                    # New unique claim
                    canonical_claims[fingerprint] = {
                        "claim_id":      fingerprint,
                        "claim_type":    claim_type,
                        "subject":       subject,
                        "object":        obj,
                        "confidence":    confidence,
                        "support_count": 1,
                        "evidence_list": [evidence],
                        "seen_in_issues": {issue_num},
                        "is_current":    True,
                        "valid_from":    evidence.get("timestamp", ""),
                        "valid_to":      None,  # None = currently true
                    }

    # Convert sets to lists and filter by quality
    result = []
    for claim in canonical_claims.values():
        claim["seen_in_issues"] = sorted(list(claim["seen_in_issues"]))
        # Quality gate: keep claims with confidence >= 0.5
        # OR supported by 2+ pieces of evidence
        if claim["confidence"] >= 0.5 or claim["support_count"] >= 2:
            result.append(claim)

    print(f"  ✅ {sum(len(d.get('claims',[])) for d in all_data)} claims → "
          f"{len(result)} unique claims after dedup")
    return result


# ════════════════════════════════════════════════════════════════════════════
# STEP 5 — Conflict & Revision Tracking
# ════════════════════════════════════════════════════════════════════════════

def track_conflicts(all_data: list[dict]) -> list:
    """
    Tracks conflicts and revisions in issue state.

    Looks at issue events to find:
    - Issues that were closed then reopened (state reversal)
    - Decisions that were reversed
    - Assignments that changed

    This is critical for 'used to be true vs currently true'.
    """
    print("\n⚡ Tracking conflicts and revisions...")

    conflicts = []
    events_dir = DATA_DIR / "events"

    for issue_data in all_data:
        issue_num = issue_data["issue_number"]
        events_file = events_dir / f"events_{issue_num}.json"

        if not events_file.exists():
            continue

        try:
            events = json.loads(events_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        # Track state changes
        state_history = []
        assignee_history = []

        for event in events:
            event_type = event.get("event", "")
            created_at = event.get("created_at", "")
            actor      = (event.get("actor") or {}).get("login", "unknown")

            if event_type in ("closed", "reopened"):
                state_history.append({
                    "state":      "closed" if event_type == "closed" else "open",
                    "changed_by": actor,
                    "timestamp":  created_at,
                })

            elif event_type == "assigned":
                assignee = event.get("assignee", {})
                if assignee:
                    assignee_history.append({
                        "assigned_to": assignee.get("login", "unknown"),
                        "assigned_by": actor,
                        "timestamp":   created_at,
                    })

            elif event_type == "unassigned":
                assignee = event.get("assignee", {})
                if assignee:
                    assignee_history.append({
                        "unassigned":  assignee.get("login", "unknown"),
                        "by":          actor,
                        "timestamp":   created_at,
                    })

        # Record conflicts if state changed more than once (reopened)
        if len(state_history) > 1:
            conflicts.append({
                "conflict_type": "state_reversal",
                "issue_number":  issue_num,
                "description":   f"Issue #{issue_num} changed state {len(state_history)} times",
                "history":       state_history,
                "current_state": issue_data["issue_state"],
                "is_current":    True,
            })

        # Record assignment changes
        if len(assignee_history) > 1:
            conflicts.append({
                "conflict_type": "assignment_change",
                "issue_number":  issue_num,
                "description":   f"Issue #{issue_num} had {len(assignee_history)} assignment changes",
                "history":       assignee_history,
            })

    print(f"  ✅ Found {len(conflicts)} conflicts/revisions")
    return conflicts


# ════════════════════════════════════════════════════════════════════════════
# STEP 6 — Save all outputs
# ════════════════════════════════════════════════════════════════════════════

def save_outputs(persons, components, claims, conflicts, merge_log):
    """Save all deduped outputs to data/deduped/"""

    # Canonical entities
    entities = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "persons":      list(persons.values()),
        "components":   list(components.values()),
        "stats": {
            "unique_persons":    len(persons),
            "unique_components": len(components),
        }
    }
    (DEDUPED / "canonical_entities.json").write_text(
        json.dumps(entities, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Canonical claims
    claims_output = {
        "generated_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_claims":  len(claims),
        "claims":        claims,
    }
    (DEDUPED / "canonical_claims.json").write_text(
        json.dumps(claims_output, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Conflicts
    conflicts_output = {
        "generated_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_conflicts": len(conflicts),
        "conflicts":       conflicts,
    }
    (DEDUPED / "conflicts.json").write_text(
        json.dumps(conflicts_output, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Merge log (audit trail)
    log_output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_merges": len(merge_log),
        "merges":       merge_log,
    }
    (DEDUPED / "merge_log.json").write_text(
        json.dumps(log_output, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n📁 Saved to data/deduped/:")
    print(f"   canonical_entities.json  ({len(persons)} persons, {len(components)} components)")
    print(f"   canonical_claims.json    ({len(claims)} claims)")
    print(f"   conflicts.json           ({len(conflicts)} conflicts)")
    print(f"   merge_log.json           ({len(merge_log)} merge records)")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Layer10 — Deduplication Pipeline")
    print("=" * 60)

    merge_log = []  # audit trail of all merges

    # Load all extracted data
    all_data = load_all_extractions()

    # Step 2: Dedup persons
    persons = dedup_persons(all_data, merge_log)

    # Step 3: Canonicalize components
    components, name_to_canonical = canonicalize_components(all_data, merge_log)

    # Step 4: Dedup claims
    claims = dedup_claims(all_data, name_to_canonical, merge_log)

    # Step 5: Track conflicts/revisions
    conflicts = track_conflicts(all_data)

    # Step 6: Save everything
    save_outputs(persons, components, claims, conflicts, merge_log)

    print("\n" + "=" * 60)
    print("  Deduplication Complete!")
    print(f"  🔀 Total merges logged: {len(merge_log)}")
    print("  Next step: python build_graph.py")
    print("=" * 60)