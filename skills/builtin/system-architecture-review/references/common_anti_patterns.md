# Common Architecture Anti-Patterns

A catalog of recurring design mistakes with real-world examples.

---

## Distributed Monolith
**Symptom**: Services that share a database. Deploying Service A requires coordinating with Service B because they both rely on the same schema.
**Example**: 12 microservices all reading/writing to the same PostgreSQL instance. A schema change by Team A breaks Team B's service.
**Fix**: One database per service. Use APIs for cross-service data access. Accept eventual consistency where strong consistency isn't required.

## Single Point of Failure (SPOF)
**Symptom**: One component whose failure cascades to a full outage.
**Example**: A single Redis instance used as both cache and session store. Redis goes down → all user sessions lost + cache misses flood the database.
**Fix**: Redis Cluster with replicas. Separate cache (recreatable) from sessions (must survive). Circuit breaker on cache access.

## Missing Back-Pressure
**Symptom**: No rate limiting between services. A slow downstream causes upstream thread pool exhaustion.
**Example**: Service A calls Service B synchronously. Service B slows down under load. Service A's threads all block waiting for B. Service A becomes unresponsive even for requests that don't need B.
**Fix**: Circuit breaker (fail fast after N failures). Bulkhead (separate thread pools). Rate limiter. Async messaging for non-blocking work.

## Secret in Plaintext
**Symptom**: API keys, database passwords, or tokens in source code, config files, or environment variables committed to git.
**Example**: `DB_PASSWORD=mysecret123` in docker-compose.yml committed to the repo. Former employee's laptop has a copy.
**Fix**: HashiCorp Vault, AWS Secrets Manager, or SOPS for encrypted secrets in git. Rotate all exposed credentials immediately.

## Synchronous Call Chain
**Symptom**: Deep synchronous chains: A → B → C → D. Each hop adds latency. Any hop failure fails the entire request.
**Example**: API Gateway → Auth Service → User Service → Notification Service → Email Provider. A slow email provider causes API timeouts for login requests.
**Fix**: Make notifications async (queue). Set timeouts at each hop. Return partial results when downstreams are degraded.

## Log-and-Forget
**Symptom**: Errors are logged but never alerted on. Teams discover problems from user complaints, not monitoring.
**Example**: Payment processing fails with "Error: timeout" logged at ERROR level. No alert fires. 500 failed transactions before a customer tweets about it.
**Fix**: Alert on error rate > threshold. Every ERROR log in a critical path must have a corresponding alert. Runbooks linked from alert messages.

## No Kill Switch
**Symptom**: No way to quickly disable a broken feature. Fix requires a full deployment.
**Example**: New recommendation algorithm causes 10x database load. No feature flag to turn it off. Team rushes a hotfix deployment (30 min) while database thrashes.
**Fix**: Feature flags for all new functionality. Kill switch that can be toggled without deployment. Automated rollback when error rate spikes.

## Big Ball of Mud
**Symptom**: No clear module boundaries. Everything depends on everything. Changing one file requires understanding the entire codebase.
**Example**: A 15,000-line `utils.py` imported by every service. Adding a function to it triggers a full CI rebuild of the entire monorepo.
**Fix**: Gradual extraction. Define module boundaries. Dependency inversion — depend on interfaces, not concrete utils. Use lint rules to enforce layering.
