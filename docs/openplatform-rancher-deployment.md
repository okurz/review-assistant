# OpenPlatform Rancher Prime deployment

Operator runbook for the review-assistant workload in the SUSE OpenPlatform Rancher Prime cluster (the OpenPlatform Rancher Prime dashboard). Manifests live in `deploy/kubernetes/`. Tracker: https://progress.opensuse.org/issues/210027

Run commands from the repository root with a kubeconfig for the target cluster. Stop before any live change that the plan gates on approval (`tasks/002_poo210027_automated_model_review_in_openplatform_container.md`, `## Approvals`).

## Access

1. Log in to the OpenPlatform Rancher Prime dashboard.
2. Open the cluster and download its kubeconfig from the cluster menu, or use `kubectl` through the Rancher CLI login.
3. Confirm the context: `kubectl config current-context` and `kubectl get ns review-assistant`.

In the Rancher UI, the workloads are under the Cluster Explorer, namespace `review-assistant`, in the Workload menu (CronJobs, Jobs, Pods) and the Storage menu (PersistentVolumeClaims). Menu names depend on the Rancher version.

## Provision secrets

The Secret `review-assistant-secrets` holds three keys. Values never go into git.

| Key | Content | Mounted as |
|-----|---------|------------|
| `gh-write-token` | GitHub token for posting reviews | env `REVIEW_ASSISTANT_GH_TOKEN` |
| `gh-read-token` | read-only GitHub token | `/etc/review-assistant/secrets/gh-read-token` |
| `vertex-adc.json` | Google ADC file of the Vertex service account | `/etc/review-assistant/secrets/gcloud/application_default_credentials.json` |

Create the Secret without writing the values to shell history or to a file in the repository:

```
kubectl -n review-assistant create secret generic review-assistant-secrets \
  --from-file=gh-write-token=/secure/path/gh-write-token \
  --from-file=gh-read-token=/secure/path/gh-read-token \
  --from-file=vertex-adc.json=/secure/path/application_default_credentials.json
```

Alternatively, copy `deploy/kubernetes/secret-template.yaml` to `deploy/kubernetes/review-assistant-secrets.yaml` (git-ignored), replace every `REPLACE_ME`, and run `kubectl apply -f` on it. Delete the filled copy afterwards.

Or create the Secret in the Rancher UI under Storage > Secrets in namespace `review-assistant` with the same three keys.

## Deploy

1. Validate the manifests: `make check-k8s`.
2. Apply the base workload with the OpenPlatform overlay: `kubectl apply -k deploy/kubernetes/overlays/openplatform`.

This creates the namespace, ServiceAccount, ConfigMap, PersistentVolumeClaim `review-assistant-state`, NetworkPolicy and the CronJob `review-assistant`. The CronJob runs hourly in dry-run mode (no `--post`).

## One-off dry run (stage 1)

```
kubectl -n review-assistant delete job review-assistant-dry-run --ignore-not-found
kubectl apply -f deploy/kubernetes/base/job-dry-run.yaml
kubectl -n review-assistant wait --for=condition=complete job/review-assistant-dry-run --timeout=35m
kubectl -n review-assistant logs job/review-assistant-dry-run
```

Expected: exit code 0 and a generated review in the log, with no GitHub post. Check the pod status for `Error` or `CrashLoopBackOff` if the wait fails.

## Scheduled dry run (stage 2)

Apply the overlay (step 2 of Deploy) and confirm the schedule and policy:

```
kubectl -n review-assistant get cronjob review-assistant -o jsonpath='{.spec.schedule} {.spec.concurrencyPolicy} {.spec.suspend}{"\n"}'
```

Expected: `0 * * * * Forbid` with `suspend` empty or `false`. Let it run for 24 hours and check `kubectl -n review-assistant get jobs,pods` for one completed job per hour.

## Logs and history

```
kubectl -n review-assistant get cronjob,jobs,pods
kubectl -n review-assistant logs -l app.kubernetes.io/name=review-assistant --tail=200
```

Rancher UI: CronJobs and Jobs views list the run history; each pod has a Logs tab.

## Pause and resume

Pause the schedule without deleting anything:

```
kubectl -n review-assistant patch cronjob review-assistant -p '{"spec":{"suspend":true}}'
```

Resume:

```
kubectl -n review-assistant patch cronjob review-assistant -p '{"spec":{"suspend":false}}'
```

A running job is not stopped by suspend. Stop it with `kubectl -n review-assistant delete job <name>`.

## Rollback

The image tag is `latest` in `deploy/kubernetes/overlays/openplatform/kustomization.yaml`. To roll back, set `newTag` to the previous short SHA (published by `.github/workflows/container.yml`), commit that change, and run `kubectl apply -k deploy/kubernetes/overlays/openplatform`. For a manifest rollback, `git revert` the offending commit and apply again.

To disable completely: `kubectl -n review-assistant delete cronjob review-assistant`. Re-apply the overlay to restore it.

## Not covered here

- Live posting (`--post --limit 1`) is a separate approved change to the CronJob args; it is not part of this runbook.
- Cluster, node, and StorageClass administration on the target cluster is out of scope.
