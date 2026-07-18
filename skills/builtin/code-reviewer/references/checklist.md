# Code Review Checklist

## 1. Correctness (must pass)
- [ ] Does the code correctly implement the stated requirements?
- [ ] Are edge cases handled (empty input, null, negative values, overflow)?
- [ ] Is error handling present and appropriate for all I/O and network calls?
- [ ] Are there any off-by-one errors in loops / array indexing?
- [ ] Are all code paths reachable and tested?

## 2. Security
- [ ] Is user input validated and sanitised?
- [ ] Are SQL queries parameterised (no string concatenation)?
- [ ] Are secrets / API keys hardcoded in the source?
- [ ] Is authentication / authorisation enforced at every boundary?
- [ ] Are file paths validated against path traversal attacks?

## 3. Performance
- [ ] Are there N+1 query patterns?
- [ ] Is data loaded eagerly when lazy loading would cause performance issues?
- [ ] Are appropriate data structures used (list vs set vs dict)?
- [ ] Is there unnecessary object allocation in hot loops?
- [ ] Are expensive operations cached where appropriate?

## 4. Maintainability
- [ ] Are function / variable names clear and descriptive?
- [ ] Is the code self-documenting, or are comments needed for complex logic?
- [ ] Are magic numbers / strings replaced with named constants?
- [ ] Is the code DRY (no significant duplication)?
- [ ] Are functions small and single-purpose (< 50 lines)?

## 5. Testing
- [ ] Are there unit tests for core logic?
- [ ] Do tests cover happy path and error cases?
- [ ] Are external dependencies mocked appropriately?
- [ ] Is test data realistic and edge-case-inclusive?

## 6. Style & Convention
- [ ] Does the code follow the project's style guide?
- [ ] Is indentation consistent?
- [ ] Are imports organised (stdlib → third-party → local)?
- [ ] Is logging appropriate (no print() for diagnostics)?
