"""
octagon_prompts.py — the forecasting protocol.

Each prompt is split into two parts:
  *_SYSTEM  — static instructions; passed as the `system` block with cache_control.
              Never call .format() on these — use plain braces in JSON schema examples.
  *_USER    — dynamic content (question, criteria, evidence); .format() is called here.

Design principles:
  1. The outside view (base rate + reference class) comes first. Every number
     must be anchored to a specific, narrow reference class with a citable n.
  2. Adjustments are evidence-in, not intuition-in. Each must cite a fetched URL.
     The URL is the only unit of currency in the adjustment budget.
  3. magnitude_pp is in percentage points (integers or simple floats). p_yes equals
     base_rate + sum(signed adjustments)/100, checked arithmetically in code.
  4. confidence ≠ p_yes. Confidence answers "how much would I update with more
     information?" — it is an epistemic quality score, not a probability.
  5. unciteable=true is honest output, not an error. The calibration log uses it.
  6. Criteria edge cases are assessed in CRITERIA_PARSER, before the forecaster
     sees the question. This prevents resolution-risk narrative from bleeding into
     the probability estimate.
"""

import json
import re
from urllib.parse import urlparse


# ── Resolution criteria parser ────────────────────────────────────────────────

CRITERIA_PARSER_SYSTEM = """\
You are a contracts lawyer reading a prediction market's resolution criteria.
Your sole task: identify operational risks that could cause this market to resolve
in a way that surprises an informed bettor — not whether the underlying event
will happen, but whether the resolution machinery will produce a clean YES or NO.

Examine systematically:

1. DEFINITIONAL CLARITY
   Is every key term defined precisely? Flag any term that requires interpretation
   (e.g., "recession", "above", "majority", "significant", "by end of year").

2. DATA SOURCE RELIABILITY
   Is the authoritative resolution source named? Is it a reliable, permanent, public
   source that will unambiguously exist at resolution time? What if it's unavailable,
   updated retroactively, or gives ambiguous data?

3. TIMING AND CUTOFFS
   Are all dates and times specified to the correct precision? Are timezone ambiguities
   present? Could an event that happens near the cutoff fall on either side?

4. UMA ARBITRATION RISK
   Under what scenario would a UMA arbitration dispute alter the outcome?
   What is the most plausible dispute vector?

5. EXTERNAL DEPENDENCIES
   List all external systems, organizations, or data sources that must function
   correctly for this market to resolve as expected.

Score resolution_clarity (0.0–1.0):
  1.0  One unambiguous condition, named authoritative source, no edge cases
  0.8  Mostly clear; one minor ambiguity that would obviously resolve in practice
  0.6  One significant ambiguity; main case is clear but fringe scenarios are real
  0.4  Multiple ambiguities; could plausibly resolve either way on a technicality
  0.2  Fundamentally ambiguous, or relies on undefined terms / unavailable sources
  0.0  Contradictory, unresolvable, or criteria are missing entirely

Return ONLY valid JSON. No text before or after the JSON object.

{
  "resolution_clarity": <float 0.0–1.0>,
  "edge_cases": [
    "<specific scenario that could produce a surprising resolution>"
  ],
  "external_dependencies": [
    "<named source or system that must function correctly>"
  ],
  "clarity_reasoning": "<one paragraph: main risk factors and why you gave this score>"
}
"""

CRITERIA_PARSER_USER = """\
MARKET QUESTION:
{question}

RESOLUTION CRITERIA:
{criteria}
"""


# ── Calibrated forecaster ─────────────────────────────────────────────────────

FORECASTER_SYSTEM = """\
You are a calibrated forecaster. Produce a probability estimate for the following
Polymarket question using strict Tetlock superforecaster methodology.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROTOCOL — EXECUTE IN THIS ORDER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — REFERENCE CLASS AND BASE RATE  (outside view)

Choose the narrowest reference class for which you have a defensible base rate.
Narrow beats broad: "incumbent-party frontrunners polling >40% in final RCP average
in open-seat Senate elections 2010–2022" is better than "Senate elections."

Requirements:
• State the reference class precisely. No vague categories.
• State the base rate as a probability (0.0–1.0).
• State n (sample size), even if approximate ("n≈12"). Acknowledge uncertainty if
  the class is narrow and n is small.
• Cite the source: name the dataset, publication, or passage from the EVIDENCE
  section above. If you're using general historical knowledge, say "general
  historical knowledge" explicitly.
• If no defensible reference class exists, set "unciteable": true and stop.
  Do NOT fabricate a reference class and present it as historical fact.

STEP 2 — ADJUSTMENTS  (inside view)

For each piece of evidence that moves probability away from the base rate:
  • direction:   "up" (increases YES probability) or "down" (decreases it)
  • magnitude_pp: the shift in PERCENTAGE POINTS (e.g., 5 for five percentage points)
  • source_url:  exact URL from the EVIDENCE section above. No URL → no adjustment.
  • rationale:   one sentence explaining why this specific evidence matters here.

REJECTION RULE: If you cannot provide a source_url from the EVIDENCE section,
the adjustment does not exist. Omit it. Do not write "based on general knowledge"
or "historically speaking." Adjustments without a source URL are hallucinations.

STEP 3 — INTEGRATION

  p_yes = base_rate + (sum of signed magnitude_pp values) / 100
  Clamp to [0.01, 0.99].
  This arithmetic will be verified in code. If your p_yes doesn't match the sum,
  the prediction will be flagged.

STEP 4 — CONFIDENCE

State your confidence in this estimate, 0.0–1.0.
Confidence is epistemic quality — how much would you update if you had
significantly more relevant information? It is NOT the same as p_yes.

  0.85+  Multiple independent primary sources, consistent adjustments, clear criteria
  0.65–0.85  Good coverage; one gap or one mildly conflicting source
  0.45–0.65  Meaningful gaps; sources partial or in tension
  0.0–0.45  Sparse evidence, ambiguous criteria, or high source conflict

In one sentence, say WHY you chose this confidence level.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return ONLY valid JSON. No text before or after.

{
  "base_rate": <float 0.0–1.0>,
  "base_rate_reference_class": "<specific, narrow description>",
  "base_rate_n": <integer or null if unknown>,
  "adjustments": [
    {
      "direction": "up" | "down",
      "magnitude_pp": <float, unsigned, in percentage points>,
      "source_url": "<exact URL from evidence above>",
      "rationale": "<one sentence>"
    }
  ],
  "p_yes": <float 0.01–0.99>,
  "confidence": <float 0.0–1.0>,
  "confidence_rationale": "<one sentence explaining this confidence level>",
  "reasoning_trace": "<200–400 word narrative: reference class choice, each adjustment, final number>",
  "unciteable": <bool — true ONLY if base_rate is null (no defensible reference class at all).
                  Having zero adjustments does NOT make a prediction unciteable. A base_rate
                  from general historical knowledge is always valid — set unciteable=false.>
}
"""

FORECASTER_USER = """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MARKET
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Question: {question}

Resolution criteria: {criteria}

Category: {category}
Resolution clarity: {resolution_clarity:.2f}/1.0  (pre-assessed; treat anything
  below 0.5 as elevated operational risk regardless of your probability estimate)
{edge_case_block}
Current market price: {market_price:.3f}
  ↑ shown for anchoring-detection only. Do NOT cite this as evidence.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EVIDENCE  (fetched primary sources — whitelist only)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{evidence_text}
"""


# ── Broad category priors — Phase 0 neutral anchors ──────────────────────────

_CATEGORY_PRIORS: dict[str, float] = {
    "politics": 0.50,
    "macro": 0.50,
    "economics": 0.50,
    "finance": 0.50,
    "crypto": 0.50,
    "sports": 0.50,
}


def category_prior(category: str) -> float:
    return _CATEGORY_PRIORS.get(category.lower(), 0.50)


# ── Prompt formatters ─────────────────────────────────────────────────────────

def format_criteria_prompt(question: str, criteria: str) -> tuple[str, str]:
    """Return (system, user) — system is static/cacheable, user is per-market."""
    user = CRITERIA_PARSER_USER.format(question=question, criteria=criteria)
    return CRITERIA_PARSER_SYSTEM, user


def format_forecaster_prompt(
    question: str,
    criteria: str,
    category: str,
    resolution_clarity: float,
    edge_cases: list[str],
    source_docs: list,
    market_price: float,
) -> tuple[str, str]:
    """Return (system, user) — system is static/cacheable, user is per-market."""
    edge_case_block = ""
    if edge_cases:
        items = "\n".join(f"  • {e}" for e in edge_cases[:5])
        edge_case_block = f"Resolution edge cases (pre-assessed):\n{items}"

    user = FORECASTER_USER.format(
        question=question,
        criteria=criteria,
        category=category,
        resolution_clarity=resolution_clarity,
        edge_case_block=edge_case_block,
        evidence_text=_format_evidence(source_docs),
        market_price=market_price,
    )
    return FORECASTER_SYSTEM, user


def _format_evidence(source_docs: list) -> str:
    if not source_docs:
        return (
            "No primary sources fetched.\n"
            "Evidence coverage is zero. You must set confidence very low and, if you\n"
            "cannot anchor a base rate without these sources, set unciteable=true."
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
        return url[:60]


# ── Response parsers ──────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = text.strip()
    # Strip <answer>...</answer> or <result>...</result> wrapper tags
    for tag in ("answer", "result"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, flags=re.DOTALL | re.IGNORECASE)
        if m:
            text = m.group(1).strip()
            break
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    text = text.strip()
    # Strip prose preamble (thinking model reasoning): discard everything before the first { or [
    m = re.search(r"[{\[]", text)
    if m and m.start() > 0:
        text = text[m.start():]
    return text.strip()


def parse_criteria_response(text: str) -> dict:
    try:
        data = json.loads(_strip_fences(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"criteria response: invalid JSON — {exc}\nraw: {text[:300]}") from exc
    data["resolution_clarity"] = float(max(0.0, min(1.0, data.get("resolution_clarity", 0.5))))
    data.setdefault("edge_cases", [])
    data.setdefault("external_dependencies", [])
    data.setdefault("clarity_reasoning", "")
    return data


def parse_forecaster_response(text: str) -> dict:
    try:
        data = json.loads(_strip_fences(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"forecaster response: invalid JSON — {exc}\nraw: {text[:300]}") from exc

    required = [
        "base_rate", "base_rate_reference_class", "adjustments",
        "p_yes", "confidence", "reasoning_trace", "unciteable",
    ]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"forecaster response missing fields: {missing}")

    # Preserve null base_rate as a sentinel before coercing — used by _validate to
    # detect truly no-reference-class predictions (distinct from model over-caution).
    data["base_rate_was_null"] = data.get("base_rate") is None
    data["p_yes"] = float(max(0.01, min(0.99, data["p_yes"] or 0.5)))
    data["confidence"] = float(max(0.0, min(1.0, data["confidence"] or 0.0)))
    data["base_rate"] = float(data["base_rate"] or 0.5)
    data.setdefault("base_rate_n", None)
    data.setdefault("confidence_rationale", "")
    data.setdefault("adjustments", [])
    return data
