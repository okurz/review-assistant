# OpenPlatform Rancher Prime deployment

Operator runbook for the review-assistant workload in the SUSE OpenPlatform Rancher Prime cluster. Manifests live in `deploy/kubernetes/`. Tracker: https://progress.opensuse.org/issues/210027

The workload is a single-replica `Deployment` running `review-assistant daemon`, which polls open `os-autoinst/openQA` pull requests every 60 seconds and reviews them one at a time.

Run commands from the repository root with a kubeconfig for the target cluster. Stop before any live change that the tracker gates on approval (https://progress.opensuse.org/issues/210027).

## Access

1. Log in to the OpenPlatform Rancher Prime dashboard.
2. Open the cluster and download its kubeconfig from the cluster menu, or use `kubectl` through the Rancher CLI login.
3. Confirm the context: `kubectl config current-context` and `kubectl get ns review-assistant`.

In the Rancher UI, the workload is under the Cluster Explorer, namespace `review-assistant`, in the Workload menu (Deployments, Pods, Jobs) and the Storage menu (PersistentVolumeClaims). Menu names depend on the Rancher version.

## Placeholders

The repository does not contain the GCP project ID or the cluster host. Before deploying, replace `REPLACE_WITH_GCP_PROJECT_ID`:

- In the repository working tree, before `kubectl apply --kustomize`: `deploy/kubernetes/base/configmap.yaml`, the `hermes-config.yaml` key. Do not commit the edited value. Applying the overlay with the placeholder resets an in-cluster edit.
- For local runs: `deploy/hermes/config.yaml`. Do not commit the edited value.

## Provision secrets

The Secret `review-assistant-secrets` holds four keys. Values never go into git.

| Key | Content | Mounted as |
|-----|---------|------------|
| `gh-write-token` | GitHub token for posting reviews | env `REVIEW_ASSISTANT_GH_TOKEN` |
| `gh-read-token` | read-only GitHub token | env `REVIEW_ASSISTANT_GH_READ_TOKEN` |
| `helmcode-api-key` | Helmcode API key of the review route | env `REVIEW_ASSISTANT_INFERENCE_KEY` |
| `vertex-adc.json` | Google ADC file of the Vertex service account | `/etc/review-assistant/secrets/gcloud/application_default_credentials.json` |

`helmcode-api-key` is the only key the shipped ConfigMap needs. `vertex-adc.json` stays because the deployment mounts it
for a switch back to the Vertex Gemini route; the current `custom` provider reads no Google credential.

Create the Secret without writing the values to shell history or to a file in the repository:

```
kubectl --namespace review-assistant create secret generic review-assistant-secrets \
  --from-file=gh-write-token=/secure/path/gh-write-token \
  --from-file=gh-read-token=/secure/path/gh-read-token \
  --from-file=helmcode-api-key=/secure/path/helmcode-api-key \
  --from-file=vertex-adc.json=/secure/path/application_default_credentials.json
```

Alternatively, copy `deploy/kubernetes/secret-template.yaml` to `deploy/kubernetes/review-assistant-secrets.yaml` (git-ignored), replace every `REPLACE_ME`, and run `kubectl apply --filename` on it. Delete the filled copy afterwards.

Or create the Secret in the Rancher UI under Storage > Secrets in namespace `review-assistant` with the same four keys.

## Deploy

1. Validate the manifests: `make check-k8s`.
2. Create the namespace as a project owner: apply `deploy/kubernetes/base/namespace.yaml` or create it in the Rancher UI. The namespace must already belong to the target Rancher project; the overlay does not create or move it.
3. Run the one-off dry run first (stage 1 below).
4. Apply the workload with the OpenPlatform overlay: `kubectl apply --kustomize deploy/kubernetes/overlays/openplatform`.

The overlay pins `ghcr.io/os-autoinst/review-assistant:latest` and sets `pilot_cap: 3` for `provider: custom`,
`model: deepseek-v4-flash` and `api_base: https://api.helmcode.com/v1`. It creates the ServiceAccount, ConfigMap, PersistentVolumeClaim `review-assistant-state`, NetworkPolicy and the Deployment `review-assistant` in the existing namespace. The Deployment starts the daemon in dry-run mode (no `--post`), so it prints reviews and does not post them.

Local prototype targets are intentionally kept outside this repository; use an operator-specific overlay and image tag for those.

## Model route

The ConfigMap carries `provider: custom`, `model: deepseek-v4-flash` and `api_base: https://api.helmcode.com/v1`, the
same pair the operator's opencode and pi run. Hermes reaches that endpoint through a loopback proxy that holds the key
from `REVIEW_ASSISTANT_INFERENCE_KEY`; the agent sees only the sentinel `CUSTOM_API_KEY=helmcode-proxy`, and
`check-isolation` asserts exactly that. The proxy binds `127.0.0.1` only, so it needs no Service and no ingress, and the
endpoint is public, so the existing `0.0.0.0/0:443` egress rule covers it.

To switch back to Gemini, set `provider: google-vertex`, `model: google/gemini-3.8-flash` and remove `api_base` from
the ConfigMap's `reviewer.yaml`, then set the real project in `hermes-config.yaml`. The ADC mount stays in place.

## One-off dry run (stage 1)

Use this before the Deployment to check connectivity, secrets and one review end to end. The dry-run overlay includes the same ServiceAccount, ConfigMap, PVC and NetworkPolicy, adds the Job, and pins the Deployment to `replicas: 0` so the daemon does not start:

```
kubectl apply --kustomize deploy/kubernetes/overlays/openplatform-dry-run
kubectl --namespace review-assistant wait --for=condition=complete job/review-assistant-dry-run --timeout=35m
kubectl --namespace review-assistant logs job/review-assistant-dry-run
```

Expected: exit code 0 and a generated review in the log, with no GitHub post. Check the pod status for `Error` or `CrashLoopBackOff` if the wait fails.

To rerun the Job, delete it first and reapply the dry-run overlay. Reapplying the dry-run overlay after stage 2 scales the Deployment back to 0.

### Status of the DeepSeek route

The Helmcode route is verified end to end up to the review gate, not to an accepted review:

- The image `v0.1.3` (commit `47fa76b`) builds and publishes through the `container` workflow.
- The dry-run Job reaches the model through the loopback proxy and produces a review draft; the API key does not appear
  in the pod spec, the Job spec or the logs.
- `review-lint` rejects that draft (an `APPROVE` that carries findings, and an over-long sentence), so the Job exits
  non-zero (`failed` reaches the backoff limit). No review is recorded and nothing is posted.

Until the model output passes `review-lint`, treat this route as a dry-run prototype. Do not run with `--post`. The
fix belongs in the review prompt or the output handling, not in `review-lint`.

## Continuous dry run (stage 2)

After stage 1 passes, apply the normal overlay and confirm the Deployment is up:

```
kubectl apply --kustomize deploy/kubernetes/overlays/openplatform
kubectl --namespace review-assistant rollout status deployment/review-assistant --timeout=5m
kubectl --namespace review-assistant get deployment review-assistant
kubectl --namespace review-assistant get pods --selector app.kubernetes.io/name=review-assistant
```

Expected: one pod, `1/1` ready. The first log lines are the startup sweep, which reviews up to `pilot_cap` (3) pull requests per cycle and continues with the rest on later cycles; afterwards each idle poll logs nothing new, because an unchanged listing answers `304 Not Modified` and costs no rate limit. Let it run for 24 hours and check that the pod has not restarted (`RESTARTS` stays at 0).

## Logs

```
kubectl --namespace review-assistant logs deployment/review-assistant --tail=200 --follow
```

Rancher UI: the Deployment's Pods list shows status and restarts; each pod has a Logs tab.

## Heartbeat and liveness

The daemon touches `/tmp/heartbeat` on every poll cycle and before every review. The liveness probe runs `test $(($(date +%s) - $(stat -c %Y /tmp/heartbeat))) -lt 900` every 60 seconds after an initial 60 seconds, and restarts the pod after three consecutive failures.

To verify the probe by hand, print the heartbeat age; it should be under 900 seconds:

```
kubectl --namespace review-assistant exec deployment/review-assistant -- sh -c 'echo $(( $(date +%s) - $(stat -c %Y /tmp/heartbeat) ))'
```

A review that runs longer than the probe window without a heartbeat causes a restart. The retry cooldown is held in memory, so a restarted pod retries the review immediately.

## Pause and resume

Pause the daemon without deleting anything. It stops polling and reviewing, and a review in progress finishes before the pod exits (`terminationGracePeriodSeconds: 300`):

```
kubectl --namespace review-assistant scale deployment review-assistant --replicas=0
```

Resume:

```
kubectl --namespace review-assistant scale deployment review-assistant --replicas=1
```

## Rolling update

The strategy is `Recreate`: the old pod stops before the new pod starts, so two daemons never write `state.json` at the same time. Expect a short gap in reviews on each rollout, not overlapping pods.

## Rollback

The image tag is `latest` in `deploy/kubernetes/overlays/openplatform/kustomization.yaml`. To roll back, set `newTag` to the previous short SHA (published by `.github/workflows/container.yml`), commit that change, and run `kubectl apply --kustomize deploy/kubernetes/overlays/openplatform`. For a manifest rollback, `git revert` the offending commit and apply again.

To disable completely, scale to 0 as in Pause and resume, or delete the Deployment with `kubectl --namespace review-assistant delete deployment review-assistant`. Re-apply the overlay to restore it.

## Not covered here

- Live posting (`--post`) is a separate approved change to the Deployment args; it is not part of this runbook.
- Cluster, node, and StorageClass administration on the target cluster is out of scope.
