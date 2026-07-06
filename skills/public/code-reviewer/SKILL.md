---
name: code-reviewer
description: Perform systematic code review following best practices. Use this skill when asked to review, audit, or inspect code for bugs, security issues, or style problems. Supports Python, TypeScript, Go, and Rust.
license: MIT
allowed-tools:
  - file_read
  - list_files
  - grep_tool
  - glob_tool
version: "1.0"
---
# Code Reviewer Skill

## Purpose
Provide thorough, structured code reviews that catch bugs, security issues, and maintainability problems before they reach production.

## Review Dimensions
When reviewing code, examine it across these dimensions in order of priority:

### 1. Correctness (highest priority)
- Does the code do what it claims to do?
- Are edge cases handled? (null/empty inputs, boundary values, error states)
- Is there any off-by-one error, type confusion, or logic inversion?

### 2. Security
- Are there any injection vulnerabilities? (SQL, command, path traversal)
- Is user input validated and sanitized?
- Are secrets or credentials hardcoded?
- Are dependencies up to date and free of known CVEs?

### 3. Performance
- Are there O(n²) algorithms where O(n log n) would work?
- Are database queries properly indexed?
- Is there unnecessary memory allocation in hot paths?

### 4. Maintainability
- Are names clear and intention-revealing?
- Is the code DRY without being overly abstract?
- Are there adequate comments explaining *why*, not *what*?

## Workflow
1. **Read the code** — use `file_read` to examine each file thoroughly
2. **List the project** — use `list_files` to understand the structure
3. **Search for patterns** — use `grep_tool` to find common anti-patterns
4. **Categorize findings** by severity: 🔴 Critical / 🟡 Warning / 🔵 Suggestion
5. **Provide actionable fixes** — for each finding, include a concrete code suggestion

## Output Format
```markdown
## Code Review: [file/PR name]

### Summary
- Files reviewed: N
- 🔴 Critical: N | 🟡 Warning: N | 🔵 Suggestion: N

### Findings

#### 🔴 [Title] (file:line)
**Problem**: [What's wrong]
**Fix**: [Code suggestion]
```

## References
See `references/checklist.md` for a detailed review checklist.
See `references/python_patterns.md` for Python-specific anti-patterns.
