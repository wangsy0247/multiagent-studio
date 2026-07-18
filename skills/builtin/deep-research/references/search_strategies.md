# Advanced Search Strategies

Techniques for more effective web research.

## 1. Query Refinement

### Precision Narrowing
- Add `site:` operator to restrict to trusted domains: `site:github.com`
- Use `after:` for recent results: `python asyncio after:2025`
- Quote exact phrases: `"memory leak" python`

### Recall Broadening
- Remove unnecessary qualifiers
- Try synonyms and related terms
- Use `OR` for alternatives: `golang OR rust web framework`

## 2. Chaining Searches

Start broad, then narrow:

```
Search 1: "web framework benchmark 2025"
  → identify top frameworks
Search 2: "FastAPI vs Litestar performance comparison"
  → drill into specific contenders
Search 3: "FastAPI production deployment best practices"
  → go deep on the winner
```

## 3. Cross-Validation Strategy

- Always find **at least 2 independent sources** for any factual claim
- Prefer official documentation and primary sources over blog posts
- When sources conflict, report the disagreement explicitly
- Check the publication date — outdated information can be misleading

## 4. Source Quality Heuristics

| Trust Level | Source Type |
|-------------|-------------|
| High | Official docs, RFCs, peer-reviewed papers, .gov / .edu |
| Medium | Well-maintained GitHub repos, established tech blogs |
| Low | Personal blogs with no date/author, forum posts, Reddit |
| Avoid | AI-generated content farms, SEO spam, unverified Medium posts |

## 5. When to Stop Searching

- You have **2-3 credible sources** covering the core question → deliver
- You've searched 3+ variations and keep getting the same results → stop
- Search snippets already answer the question fully → synthesise, don't re-search
- 5 total tool calls reached → STOP and deliver what you have
