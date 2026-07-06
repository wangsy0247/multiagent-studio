---
name: deep-research
description: Conduct multi-source deep research with web search, fact verification, and cited reports. Use this skill when asked to research a topic thoroughly, write a report, or analyze a complex subject from multiple angles.
license: MIT
allowed-tools:
  - web_search
  - web_fetch
  - file_read
  - file_write
  - list_files
version: "2.0"
---
# Deep Research Skill

## Purpose
Produce well-researched, fact-checked reports by gathering information from multiple independent sources, cross-referencing claims, and synthesizing findings into a structured output.

## Workflow

### Phase 1: Scoping
1. Clarify the research question — what exactly needs to be answered?
2. Identify key subtopics and angles to explore
3. Determine the expected depth (quick overview vs. exhaustive report)

### Phase 2: Multi-Source Search
1. **Broad search**: Use `web_search` with 3-5 different query formulations to capture diverse perspectives
2. **Deep dive**: For the most promising results, use `web_fetch` to read the full content
3. **Cross-reference**: Verify key claims against at least 2 independent sources
4. **Gap analysis**: Identify what's missing and search specifically for those gaps

### Phase 3: Synthesis
1. Organize findings by theme, not by source
2. Identify consensus views vs. minority opinions
3. Note conflicting information explicitly — don't hide uncertainty
4. Rank evidence quality: primary > secondary > tertiary

### Phase 4: Report Writing
1. Start with an executive summary (3-5 sentences)
2. Structure with clear headings
3. Include inline citations: `[citation:Source Title](URL)`
4. End with a "Sources" section listing all references

## Output Template
```markdown
# [Research Topic]

## Executive Summary
[3-5 sentence overview of key findings]

## Key Findings

### [Theme 1]
[Findings with citations]

### [Theme 2]
[Findings with citations]

## Conflicting Perspectives
[Areas of disagreement among sources]

## Limitations
[What this research couldn't cover, data quality notes]

## Sources
- [Source 1](URL) — Description
- [Source 2](URL) — Description
```

## Quality Standards
- Every factual claim must have a citation
- At least 3 independent sources per major claim
- Acknowledge uncertainty and conflicting evidence
- Distinguish between fact and opinion

## References
See `references/search_strategies.md` for advanced search techniques.
See `templates/report_template.md` for alternative report formats.
