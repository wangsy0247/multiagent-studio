# Rollback Procedures

## Database Migration Rollback

### Safe Migrations (no rollback needed)
- Adding a new column (with default value)
- Adding a new table
- Creating a new index

### Rollback Required
1. **Column rename**: `ALTER TABLE ... RENAME COLUMN ... TO ...`
   - Rollback: Reverse the rename in a new migration
   - Prevention: Never rename. Add new column → migrate data → drop old in separate deploy

2. **Column drop**: `ALTER TABLE ... DROP COLUMN ...`
   - Rollback: Restore from backup (data is gone)
   - Prevention: Soft-delete column, drop after 1 week of monitoring

3. **Data migration**: `UPDATE ... SET ...`
   - Rollback: Run the reverse UPDATE (if you saved the old values)
   - Prevention: Create a backup table before migration

## Application Rollback

### Kubernetes
```bash
# View deployment history
kubectl rollout history deployment/my-app

# Rollback to previous revision
kubectl rollout undo deployment/my-app

# Rollback to specific revision
kubectl rollout undo deployment/my-app --to-revision=3

# Verify rollback
kubectl rollout status deployment/my-app
```

### Docker Compose
```bash
# Stop current
docker-compose down

# Checkout previous version tag
git checkout v1.2.3

# Redeploy
docker-compose up -d

# Verify
docker-compose ps
docker-compose logs --tail=50
```

### AWS ECS
```bash
# Force new deployment of previous task definition
aws ecs update-service \
  --cluster my-cluster \
  --service my-service \
  --task-definition my-app:42 \
  --force-new-deployment
```

## Decision Tree

```
Issue detected after deployment
│
├── Data corruption or loss?
│   └── YES → ROLLBACK IMMEDIATELY. Investigate after.
│
├── Security breach? (credentials leaked, auth bypass, data exposure)
│   └── YES → ROLLBACK + REVOKE CREDENTIALS. Incident response protocol.
│
├── Error rate > 5% above baseline?
│   └── YES → ROLLBACK (unless fix is a 1-line config change deployable in < 2 min)
│
├── Critical user flow broken? (login, payment, core feature)
│   └── YES → ROLLBACK. User trust > feature velocity.
│
└── Non-critical regression? (cosmetic bug, minor performance dip)
    └── Fix forward. File a ticket. Deploy fix within 24 hours.
```
