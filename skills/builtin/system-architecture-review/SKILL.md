---
name: system-architecture-review
description: Conduct comprehensive system architecture reviews covering scalability, reliability, security, and cost. Use when asked to review architecture, design a system, evaluate trade-offs, or assess technical proposals against industry best practices.
license: MIT
version: "2.1"
author: multiagent-studio
---
# System Architecture Review Skill

## Purpose

Provide rigorous, structured reviews of software system architectures — whether existing systems being audited or new designs being proposed. This skill enforces a systematic methodology that catches design flaws before they become production incidents.

## When to Use

- "Review this architecture"
- "Is this system design sound?"
- "Evaluate the trade-offs between X and Y"
- "What are the risks in this proposal?"
- "Design a system for [requirements]"

## Review Dimensions (in priority order)

### 1. Correctness & Completeness (Critical)
Does the design solve the stated problem? Are there gaps?

- **Requirements coverage**: Map every requirement to a design component. Flag uncovered requirements.
- **Data consistency**: Where is the source of truth? What consistency model (strong, eventual, causal)? Are there split-brain risks?
- **Failure modes**: For each component, ask: "What happens when this fails?" Trace the blast radius.
- **Edge cases**: Peak load 10x normal, network partition, clock skew, distributed transaction failure.

### 2. Scalability (Critical)
Will this system handle growth without rewriting?

- **Bottleneck identification**: Database connection pool, single-threaded queue consumer, shared state.
- **Horizontal vs vertical**: Which components scale out? Which scale up? Are there hard limits?
- **Data growth**: 1 year projection at current growth rate. Does the schema/index strategy hold?
- **Caching strategy**: What's cached? Where? TTL? Invalidation strategy? Cache stampede protection?

### 3. Reliability & Resilience (Critical)
How does the system behave under stress and failure?

- **SPOF analysis**: Every component → is it a single point of failure? Can it be made redundant?
- **Circuit breakers**: Are downstream failures isolated? Timeouts configured? Retry with backoff?
- **Graceful degradation**: What features can be sacrificed to keep core functionality running?
- **Data durability**: Replication factor, backup strategy, RPO/RTO targets, recovery tested?

### 4. Security (High)
- **Trust boundaries**: Draw the lines. What crosses them? Is authentication enforced at every boundary?
- **Least privilege**: Does each service have only the permissions it needs? Database credentials per service?
- **Secret management**: How are secrets rotated? Are any hardcoded? Environment variables vs vault?
- **Attack surface**: Public endpoints, third-party dependencies, supply chain risk.

### 5. Observability (High)
- **Logging**: Structured? Correlation IDs propagated across services? Log levels appropriate?
- **Metrics**: RED metrics (Rate, Errors, Duration) for every service. Business KPIs instrumented?
- **Alerting**: Alert on symptoms (SLO breaches), not causes. Runbooks exist for each alert?
- **Tracing**: Distributed tracing configured? Sampling rate appropriate for the traffic level?

### 6. Cost & Operations (Medium)
- **Cost drivers**: What scales with users? Compute, storage, bandwidth, API calls to third parties?
- **Operational toil**: What manual interventions does this design require? Can they be automated?
- **Deployment complexity**: How many services? Orchestration? Rollback strategy?

### 7. Maintainability (Medium)
- **Coupling**: Shared databases? Synchronous chains? Cascading failures from tight coupling?
- **Technology diversity**: Is each technology choice justified? Too many languages/frameworks?
- **Documentation**: Architecture Decision Records (ADRs) for key choices?

## Workflow

### Phase 1: Information Gathering
1. **Collect artifacts**: Use `file_read` to load architecture docs, diagrams, ADRs, API specs
2. **Map the system**: Use `list_files` and `glob_tool` to discover codebase structure
3. **Identify stakeholders**: Who are the users? What are their SLO expectations?
4. **Load the checklist**: Read `references/review_checklist.md` for the detailed item-by-item checklist

### Phase 2: Deep Analysis
1. **Trace critical paths**: Follow a request from entry to response. Note every hop.
2. **Stress-test the design mentally**: "What happens at 10x load? During a regional outage?"
3. **Cross-reference with checklist**: Use `references/review_checklist.md` systematically
4. **Search for anti-patterns**: Use `grep_tool` to find known bad patterns in code

### Phase 3: Report Generation
1. **Prioritize findings**: Critical (must fix before production) > High > Medium > Low
2. **Write actionable recommendations**: Each finding must include a concrete fix, not just criticism
3. **Use the report template**: Read `templates/architecture_report.md` for the output format
4. **Distinguish facts from opinions**: Clearly mark inferences and assumptions

### Phase 4: Validation
1. **Self-review**: Would I bet my reputation on this review? What did I miss?
2. **Check for balance**: Did I acknowledge what the design does well?
3. **Estimate effort**: For each recommendation, ballpark the implementation effort (S/M/L/XL)

## Output Format

Always use the template from `templates/architecture_report.md`:
```markdown
# Architecture Review: [System Name]

## Executive Summary
[3-5 sentence verdict. Start with the most important finding.]

## Risk Matrix
| Dimension | Score (1-5) | Key Concern |
|-----------|-------------|-------------|
| Correctness | ? | ? |
| Scalability | ? | ? |
| Reliability | ? | ? |
| Security | ? | ? |
| Observability | ? | ? |
| Cost/Ops | ? | ? |
| Maintainability | ? | ? |

## Critical Findings (must fix before production)
### [Finding 1] — Severity: Critical
**Problem**: ...
**Impact**: ...
**Recommendation**: ...
**Effort**: [S/M/L/XL]

## High-Priority Findings
...

## What This Architecture Does Well
...

## Appendix: Assumptions & Methodology
...
```

## Anti-Patterns to Flag

- **Distributed monolith**: Services that share a database and can't deploy independently
- **Single point of failure**: Any component whose failure takes down the system
- **Missing back-pressure**: No rate limiting or circuit breaking between services
- **Secret in plaintext**: API keys, passwords, or tokens in code or config files
- **Synchronous chains**: A→B→C→D synchronous call chain where any link failure cascades
- **No kill switches**: Missing feature flags or emergency disable mechanisms
- **Log-and-forget**: Errors logged but not alerted on — silent failures

## References
- `references/review_checklist.md` — comprehensive 50-point architecture review checklist
- `references/common_anti_patterns.md` — catalog of common architectural mistakes with examples
- `templates/architecture_report.md` — the standard report template to use for all reviews
