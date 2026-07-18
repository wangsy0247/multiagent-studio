# Architecture Review Report Template

Use this template for ALL architecture review outputs.
Replace `[placeholders]` with actual findings.

---

# Architecture Review: [System Name]

**Reviewer**: AI Architecture Reviewer (system-architecture-review skill)
**Date**: [date]
**Artifacts Reviewed**: [list of documents, diagrams, code repositories]
**Review Version**: 1.0

---

## Executive Summary

[3-5 sentences that answer:]

1. **Verdict**: Is this architecture ready for production? (Yes / With Changes / No)
2. **Top Risk**: What's the single biggest concern?
3. **Key Strength**: What does this design do exceptionally well?
4. **Effort to Remediate**: Rough estimate (days / weeks / months)

---

## Risk Matrix

| Dimension | Score (1–5) | Confidence | Key Concern |
|-----------|-------------|------------|-------------|
| Correctness | ? | High/Med/Low | [one-line summary] |
| Scalability | ? | High/Med/Low | [one-line summary] |
| Reliability | ? | High/Med/Low | [one-line summary] |
| Security | ? | High/Med/Low | [one-line summary] |
| Observability | ? | High/Med/Low | [one-line summary] |
| Cost & Operations | ? | High/Med/Low | [one-line summary] |
| Maintainability | ? | High/Med/Low | [one-line summary] |

**Scoring**: 1 = Critical gap, 3 = Adequate, 5 = Exemplary
**Confidence**: High = verified by code/docs, Low = inferred from limited information

---

## Critical Findings

*Items that MUST be addressed before production deployment.*

### [C-1] [Title] — Severity: Critical

**Category**: [Correctness / Scalability / Reliability / Security / Observability / Operations / Maintainability]

**Problem Statement**:
[Describe the finding in plain language. Assume the reader is a senior engineer who wasn't in the review meeting.]

**Evidence**:
- [Specific file, diagram, or statement that demonstrates the problem]
- [Quantify the impact where possible: "This query does a full table scan on 10M rows"]

**Impact**:
- **Now**: [What happens today?]
- **Under Load**: [What happens at 2x, 5x, 10x current load?]
- **Failure Mode**: [What's the worst case? Data loss? Full outage? Security breach?]

**Recommendation**:
[Concrete, actionable fix. Include code snippets or configuration examples.]

**Effort Estimate**: [S (< 1 day) / M (1–3 days) / L (1–2 weeks) / XL (> 2 weeks)]
**Alternative Considered**: [If applicable, what other approaches were evaluated and why rejected?]

---

## High-Priority Findings

*Items that should be addressed within the next 1–2 sprints.*

### [H-1] ...
[Same format as Critical, but for non-blocking concerns]

---

## Medium-Priority Findings

*Items for the backlog. Important but not urgent.*

### [M-1] ...
[Brief description + recommendation. Shorter format acceptable.]

---

## Low-Priority / Nice-to-Have

*Improvements worth considering when time allows.*

- [Bullet points sufficient for low-priority items]

---

## What This Architecture Does Well

*Explicitly acknowledge strengths. Blind criticism erodes trust.*

1. **[Strength 1]**: [Why this matters]
2. **[Strength 2]**: [Why this matters]

---

## Appendix

### A. Review Methodology
- Reviewed artifacts: [list]
- Review dimensions applied: [list from SKILL.md]
- Checklist completion: [X/50 items assessed from references/review_checklist.md]
- Tools used: [file_read, grep_tool, etc.]

### B. Assumptions
- [List assumptions made during review where evidence was incomplete]
- [Mark confidence: High / Medium / Low for each assumption]

### C. Out of Scope
- [Items explicitly excluded from this review]
- [Recommended follow-up reviews]

---

*Report generated using the system-architecture-review skill. For methodology details, see the skill's references/review_checklist.md.*
