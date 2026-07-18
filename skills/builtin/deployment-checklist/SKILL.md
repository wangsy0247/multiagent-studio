---
name: deployment-checklist
description: Production deployment safety checklist — pre-flight checks, canary validation, rollback procedures. Use when preparing to deploy, before merging to main, or when asked about deployment safety practices.
license: MIT
version: "1.0"
author: multiagent-studio
---
# Deployment Checklist Skill

## Purpose

Prevent production incidents by enforcing a systematic pre-deployment verification process. This skill guides you through every safety check before, during, and after a deployment.

## When to Use

- "I'm about to deploy, what should I check?"
- "Review this deployment for safety"
- "What's our rollback plan?"
- Before merging a PR to main/master

## Workflow

### Phase 1: Pre-Flight (Before Any Changes)

1. **Verify CI/CD pipeline**: All tests green? Linting passed? Security scan clean?
2. **Check dependencies**: Any deprecated packages? Known CVEs in new dependencies?
3. **Database migrations**: Are they backward-compatible? (Never rename a column — add new, migrate data, drop old in separate deploy)
4. **Configuration changes**: Any new env vars? Are they set in all environments? Secrets rotated?
5. **Load test results**: If this is a performance-sensitive change, have you load tested?
6. **Read the deployment script**: Check `scripts/deploy.sh` or equivalent for any hardcoded assumptions

### Phase 2: Canary / Staged Rollout

1. **Deploy to staging first**: Verify with production-like data
2. **Smoke tests**: Hit critical endpoints. Check response times, error rates.
3. **Canary deployment**: 5% → 25% → 50% → 100%, with 5-minute observation windows
4. **Monitor dashboards**: Watch RED metrics. Compare to baseline (same time last week).
5. **Check error logs**: Any new error patterns? Increased rate of known errors?

### Phase 3: Post-Deployment Validation

1. **Business metrics**: Are key flows working? Signups, purchases, API calls?
2. **Alert silence**: No new alerts firing. If alerts fire, assess severity before acknowledging.
3. **Customer impact**: Check support channels for user-reported issues.
4. **Performance baseline**: Compare p50/p95/p99 latencies to pre-deployment baseline.

### Phase 4: Rollback Decision

If any Critical or High-severity issue is found:
1. **Decide within 5 minutes**: Is this a rollback situation? (Yes if: data corruption, security breach, >5% error rate increase, critical user flow broken)
2. **Execute rollback**: Use the pre-tested rollback procedure. Never "fix forward" under pressure.
3. **Post-mortem**: Within 24 hours, document: what happened, why, how detected, how prevented next time.

## Quick Reference Card

```
BEFORE:
[ ] CI green
[ ] DB migrations backward-compatible
[ ] Secrets/config in place
[ ] Load tested (if perf-sensitive)

DURING:
[ ] Staging verified
[ ] Canary: 5% → 25% → 50% → 100%
[ ] Dashboards monitored
[ ] Error logs watched

AFTER:
[ ] Business metrics normal
[ ] No new alerts
[ ] Latency baseline unchanged

IF ROLLBACK:
[ ] Decide in 5 min
[ ] Execute rollback
[ ] Post-mortem in 24h
```

## References

- `references/rollback_procedures.md` — Detailed rollback steps for common deployment patterns
- `scripts/preflight_check.sh` — Automated pre-flight verification script
