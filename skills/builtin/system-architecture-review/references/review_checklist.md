# Architecture Review Checklist — 50 Points

Use this checklist systematically during Phase 2 (Deep Analysis).
Check each item: ✅ Pass / ❌ Fail / ⚠️ Needs Investigation / N/A

---

## Correctness & Completeness (10 points)

1. [ ] Every functional requirement has an owning component
2. [ ] Every non-functional requirement has a quantified SLO
3. [ ] Data consistency model is explicitly documented per data store
4. [ ] All external API contracts are versioned
5. [ ] Idempotency keys used for all mutating operations
6. [ ] Input validation at every system boundary
7. [ ] Error responses are standardized across services
8. [ ] No silent data loss paths (truncation, overflow, dropped messages)
9. [ ] Timezone handling is explicit (UTC storage, localized display)
10. [ ] Pagination strategy exists for all list endpoints

## Scalability (8 points)

11. [ ] Horizontal scaling path identified for every stateful component
12. [ ] Database connection pooling configured with appropriate sizing
13. [ ] Read replicas strategy for read-heavy workloads
14. [ ] Caching layer with documented TTL and invalidation
15. [ ] Async processing for non-request-critical work (queues, workers)
16. [ ] No unbounded collections (paginated, capped, or TTL'd)
17. [ ] Hot partition mitigation for sharded data stores
18. [ ] Load testing results or capacity model documented

## Reliability (8 points)

19. [ ] No single points of failure in the critical path
20. [ ] Circuit breakers on all external service calls
21. [ ] Retry with exponential backoff and jitter
22. [ ] Graceful degradation strategy documented
23. [ ] Health check endpoints on every service
24. [ ] Backup strategy with tested recovery procedure
25. [ ] RPO and RTO targets defined and measured
26. [ ] Multi-AZ or multi-region failover for critical services

## Security (8 points)

27. [ ] Authentication enforced at every trust boundary
28. [ ] Authorization is per-action, not just per-endpoint
29. [ ] Secrets managed via vault/manager, never in code or env files
30. [ ] Dependency scanning for known CVEs in CI pipeline
31. [ ] Rate limiting on all public endpoints
32. [ ] Input sanitization against injection attacks
33. [ ] TLS everywhere for data in transit
34. [ ] Principle of least privilege applied to service accounts

## Observability (6 points)

35. [ ] Structured logging with correlation IDs
36. [ ] RED metrics for every service endpoint
37. [ ] Alerts on SLO breaches, not individual errors
38. [ ] Distributed tracing spans across service boundaries
39. [ ] Dashboard exists for key business and technical metrics
40. [ ] Runbooks exist for every high-severity alert

## Operations (5 points)

41. [ ] Deployment is fully automated (CI/CD)
42. [ ] Rollback can be completed within SLO window
43. [ ] Database migrations are backward-compatible
44. [ ] Feature flags for gradual rollout and emergency kill switch
45. [ ] Capacity planning runbook updated quarterly

## Maintainability (5 points)

46. [ ] Architecture Decision Records for all significant design choices
47. [ ] Service boundaries follow domain boundaries (DDD-aligned)
48. [ ] No shared databases between services
49. [ ] API documentation is auto-generated from code
50. [ ] On-call rotation and escalation path documented
