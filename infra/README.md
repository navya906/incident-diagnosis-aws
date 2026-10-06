# Real test stack and fault injection

> **Not run yet.** These were written in Phase 1 but have never been deployed or executed.
> Running them costs money (ALB, RDS, Fargate, data transfer) and changes real AWS resources.
> Use a sandbox account, never production.

## 1. Deploy the test stack

Needs: AWS CLI v2, credentials for a sandbox account, a VPC with two public subnets.

```bash
aws cloudformation deploy \
  --stack-name incident-test \
  --template-file infra/test-stack/template.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides VpcId=vpc-xxxx SubnetIds=subnet-aaaa,subnet-bbbb
```

The default `AppImage` is nginx, which does not use the database. For database faults to show up
in application metrics, pass an image that reads `DB_HOST` / `DATABASE_SECRET_ARN` and queries
PostgreSQL per request.

## 2. Inject a fault (one at a time)

```bash
cd backend
pip install -e ".[aws]"
python -m app.offline.fault_injection --stack incident-test --fault lb_error_spike            # dry run
python -m app.offline.fault_injection --stack incident-test --fault lb_error_spike --execute  # real
```

Wait for symptoms and alarms (10 to 15 minutes), note the onset time, and capture telemetry with the
real collectors (Phase 2). Then revert:

```bash
python -m app.offline.fault_injection --stack incident-test --fault lb_error_spike --revert --execute
```

| Fault | Injection | Revert |
|---|---|---|
| deployment_failure | deploy a task definition that exits at startup | previous task definition |
| connection_exhaustion | hold ~120 DB connections (run inside the VPC) | ends with the run |
| function_timeout | consumer Lambda timeout 3 s + 500 messages | previous timeout |
| cpu_saturation | scale to 1 task + 64 concurrent HTTP clients | previous desired count |
| lb_error_spike | listener default action -> fixed 503 | previous default actions |
| iam_permission_failure | detach secrets policy from task role | re-attach |
| security_group_misconfiguration | revoke tcp/5432 app SG -> DB SG | re-authorize |
| db_latency_increase | 16 concurrent heavy scans (run inside the VPC) | ends with the run |
| task_crash_loop | task definition with 128 MiB hard limit that allocates 400 MiB | previous task definition |
| queue_backlog | consumer reserved concurrency 1 + 5000 messages | previous concurrency |

Revert state is saved in `backend/.fault-state/` (git-ignored).

## 3. Tear down

```bash
aws cloudformation delete-stack --stack-name incident-test
```
