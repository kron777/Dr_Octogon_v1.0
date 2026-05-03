"""
Research prompts and response parsers.

Two prompts:
  CRITERIA_PROMPT  — assesses operational/resolution risk; separate Claude call,
                     cheaper model, runs before evidence fetch so we can bail early
                     on deeply ambiguous markets.

  RESEARCH_PROMPT  — the main forecasting engine. Tetlock superforecaster methodology:
                     outside view (reference class + base rate) first, then inside-view
                     adjustments, each one citable or dropped. Produces structured JSON
                     that the calibration loop can grade directly.

Design principles baked into the prompts:
  1. Base rate must be grounded in a specific, narrow reference class with a named source.
     "Historical data" is not a citation. Fabricated reference classes are not allowed.
  2. Each adjustment is either cited from the provided evidence or it does not exist.
     The REJECTION RULE makes this a binary, not a suggestion.
  3. unciteable=true is not an error state — it is honest output. The calibration log
     uses it to measure how often Claude can actually source its estimates.
  4. Resolution-criteria edge cases are kept strictly separate from probability estimation.
     This prevents resolution ambiguity from inflating p_yes uncertainty in ways that
     contaminate the calibration signal.
  5. Market price is visible to prevent anchoring detection but explicitly banned as evidence.
"""

import json
import re
from urllib.parse import urlparse


# ── Resolution criteria assessment ───────────────────────────────────────────

CRITERIA_PROMPT = """\
You are assessing the operational risk of a prediction market's resolution criteria.
Your task is not to estimate whether the event will happen — only whether the criteria
will produce a clear, unambiguous outcome when the time comes.

MARKET QUESTION:
{question}

RESOLUTION CRITERIA:
{criteria}

Assess for operational risk. Examine each of the following:

1. Is the resolution condition precisely defined with a single clear interpretation?
2. Is the authoritative data source named? Is it reliably available at resolution time?
3. Are there timezone or cutoff ambiguities (e.g., "by end of year" — whose timezone?)?
4. Are there definitional traps (e.g., "recession", "above", "majority", "significant"
   — any term that requires interpretation beyond the plain text)?
5. What happens if the named source is unavailable, reports conflicting figures,
   or updates its data retroactively?
6. Could a UMA arbitration dispute alter the outcome? On what grounds?

Score resolution_clarity 0.0–1.0:
  1.0  Single clear condition, named authoritative source, no edge cases
  0.8  Mostly clear; one minor ambiguity that would resolve obviously in practice
  0.6  One significant ambiguity; main case is still clear but fringe cases are real
  0.4  Multiple ambiguities; the resolution could plausibly go either way on a technicality
  0.2  Fundamentally ambiguous, or relies on undefined terms or unavailable sources
  0.0  Resolution criteria are contradictory or unresolvable

Return ONLY valid JSON. No text before or after.

{{
  "resolution_clarity": <float 0.0–1.0>,
  "edge_cases": [
    {{"description": "<specific scenario that could cause an unexpected resolution>",
      "severity": "LOW" | "MEDIUM" | "HIGH"}}
  ],
  "clarity_reasoning": "<one paragraph: main risk factors and why you gave this score>"
}}
"""

# ── Main probability estimation ───────────────────────────────────────────────

RESEARCH_PROMPT = """\
You are a calibrated forecaster. Estimate the probability that this Polymarket question
resolves YES using strict Tetlock superforecaster methodology.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MARKET
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Question: {question}

Resolution criteria:
{criteria}

Category: {category}
Resolution clarity: {resolution_clarity:.2f}/1.0
{edge_case_summary}
Current market price: {market_price:.3f}  ← visible for anchoring detection only; do NOT cite as evidence

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EVIDENCE  (whitelisted primary sources only)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{evidence_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CATEGORY PRIOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Broad historical base rate for {category}: {category_base_rate:.2f}
Override this with a narrower reference class wherever possible.
A narrower class that fits is always better than a broad prior.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
METHODOLOGY — EXECUTE IN ORDER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — BASE RATE (outside view)
Find the narrowest reference class for which you have a defensible base rate.

Requirements:
• The class must be specific. Compare:
    GOOD: "US Senate incumbents trailing by 3–5 points in the final RCP average in
           the 6 elections from 2014–2024 who were running in states rated Lean R by Cook"
    BAD:  "Senate elections" or "political events"
• The base rate must come from a citable source — a named dataset, publication, or passage
  from the evidence above. If you are drawing on general historical knowledge rather than
  a source, note this explicitly and set base_rate.source = "general knowledge".
• If no defensible reference class exists at all, set base_rate.source = "none" and
  unciteable = true. Do NOT invent a reference class and present it as fact.
• Do NOT use the current market price as a base rate or as evidence of any kind.

STEP 2 — ADJUSTMENTS (inside view)
For each piece of evidence that moves probability away from the base rate:

  ✓ State direction:  UP (increases YES probability) or DOWN (decreases it)
  ✓ State magnitude:  in probability units (e.g., 0.08 = eight percentage points)
  ✓ Cite the source:  outlet name + date, or URL from the EVIDENCE section above
  ✓ Explain relevance: why does this specific piece of evidence matter for this question?

  REJECTION RULE: If you cannot cite a specific source from the EVIDENCE section,
  do NOT include the adjustment. Omit it entirely. Do not write "based on general
  knowledge" or "historically speaking." Uncited adjustments are not adjustments —
  they are hallucinations. Reject them before writing them.

STEP 3 — INTEGRATION
  p_yes_raw = base_rate.value + Σ signed adjustments
  Clamp to [0.01, 0.99].

STEP 4 — CONFIDENCE
Estimate your confidence in this p_yes estimate, 0.0–1.0.
Confidence is epistemic quality, not probability value. Ask:
  • Source coverage: was there enough primary-source evidence to work from?
  • Consistency: do adjustments point in a coherent direction, or do they cancel noisily?
  • Resolution clarity: a score below 0.5 should drag confidence down materially —
    even a perfect prediction is worth less if the market might resolve on a technicality
  • Category track record: unknown for Phase 0; treat as neutral

  0.85+   Multiple independent primary sources, consistent adjustments, clear criteria
  0.65–0.85  Good coverage; one significant gap or one conflicting source
  0.45–0.65  Meaningful evidence gaps, or sources that conflict without resolution
  0.0–0.45   Sparse evidence, fundamentally ambiguous criteria, or high source conflict

STEP 5 — EDGE CASES (resolution risk only)
List operational risk scenarios: ways the resolution criteria could produce a surprising
outcome regardless of what actually happens in the world. Focus on definitional traps,
data source failures, timing ambiguities, and UMA dispute vectors.
These are separate from your probability estimate and must not affect p_yes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return ONLY valid JSON. No text before or after the JSON object.

{{
  "p_yes": <float, 0.01–0.99>,
  "confidence": <float, 0.0–1.0>,
  "base_rate": {{
    "value": <float>,
    "reference_class": "<specific, narrow description of the class>",
    "source": "<named source, 'general knowledge', or 'none'>"
  }},
  "adjustments": [
    {{
      "direction": "UP" | "DOWN",
      "magnitude": <float, unsigned, in probability units e.g. 0.08>,
      "source": "<outlet + date, or URL from evidence above>",
      "reasoning": "<why this evidence moves the probability in this direction>"
    }}
  ],
  "edge_cases_considered": [
    {{
      "description": "<specific operational risk scenario>",
      "severity": "LOW" | "MEDIUM" | "HIGH"
    }}
  ],
  "reasoning_trace": "<200–400 word narrative: reference class selection rationale, each adjustment, final estimate and why>",
  "unciteable": <bool — true if base rate lacks a specific source OR any required adjustment was dropped for lack of citation>
}}
"""


# ── Prompt formatters ─────────────────────────────────────────────────────────

# Broad neutral priors by category — overridden by reference-class reasoning in the prompt.
# Phase 1 will replace these with calibration-adjusted values.
_CATEGORY_PRIORS: dict[str, float] = {
    "politics": 0.50,
    "macro": 0.50,
    "economics": 0.50,
    "finance": 0.50,
    "crypto": 0.50,
    "sports": 0.50,
}


def category_base_rate(category: str) -> float:
    return _CATEGORY_PRIORS.get(category.lower(), 0.50)


def format_criteria_prompt(question: str, criteria: str) -> str:
    return CRITERIA_PROMPT.format(question=question, criteria=criteria)


def format_research_prompt(
    question: str,
    criteria: str,
    category: str,
    resolution_clarity: float,
    edge_cases: list[dict],
    source_docs: list,
    market_price: float,
) -> str:
    edge_case_summary = ""
    if edge_cases:
        high = [e for e in edge_cases if e.get("severity") == "HIGH"]
        if high:
            edge_case_summary = "⚠ HIGH-severity resolution risk:\n" + "\n".join(
                f"  • {e['description']}" for e in high
            )

    return RESEARCH_PROMPT.format(
        question=question,
        criteria=criteria,
        category=category,
        resolution_clarity=resolution_clarity,
        edge_case_summary=edge_case_summary,
        evidence_text=_format_evidence(source_docs),
        category_base_rate=category_base_rate(category),
        market_price=market_price,
    )


def _format_evidence(source_docs: list) -> str:
    if not source_docs:
        return (
            "No evidence fetched. Evidence coverage is zero.\n"
            "You must note this in confidence (should be very low) and reasoning_trace."
        )
    parts = []
    for i, doc in enumerate(source_docs[:12], 1):
        domain = _domain(doc.url)
        excerpt = doc.content[:900].strip()
        parts.append(
            f"[{i}] {domain}  ({doc.source_class})\n"
            f"    URL: {doc.url}\n"
            f"    {excerpt}"
        )
    return "\n\n".join(parts)


def _domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return url[:50]


# ── Response parsers ──────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    return text.strip()


def parse_criteria_response(text: str) -> dict:
    try:
        data = json.loads(_strip_fences(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"criteria response: invalid JSON — {exc}\nraw: {text[:300]}") from exc

    data["resolution_clarity"] = float(
        max(0.0, min(1.0, data.get("resolution_clarity", 0.5)))
    )
    data.setdefault("edge_cases", [])
    data.setdefault("clarity_reasoning", "")
    return data


def parse_research_response(text: str) -> dict:
    try:
        data = json.loads(_strip_fences(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"research response: invalid JSON — {exc}\nraw: {text[:300]}") from exc

    required = [
        "p_yes", "confidence", "base_rate", "adjustments",
        "edge_cases_considered", "reasoning_trace", "unciteable",
    ]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"research response missing fields: {missing}")

    data["p_yes"] = float(max(0.01, min(0.99, data["p_yes"])))
    data["confidence"] = float(max(0.0, min(1.0, data["confidence"])))
    data.setdefault("adjustments", [])
    data.setdefault("edge_cases_considered", [])

    return data
