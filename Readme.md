# review-assistant

Automated, model-based pull request review for `os-autoinst/openQA`, run from a separate, non-personal GitHub
account. Reviews follow the `os-autoinst/openQA-skill` playbook and use `deepseek-v4-flash` through Helmcode, the
same provider and model this host runs in opencode and pi. Hermes reaches the endpoint through the `custom` provider,
whose base URL comes from `api_base` and whose key is held by the loopback proxy `review_assistant/inference_proxy.py`;
the agent itself only ever sees the non-secret sentinel `helmcode-proxy`. Only `COMMENT` and `APPROVE` events are
allowed (D1, D5); merging is out of scope.

Status: pilot. OpenPlatform deployment runbook: `docs/openplatform-rancher-deployment.md`. Tracker:
https://progress.opensuse.org/issues/210027

## Setup on this host

1. Operator config, no credentials: `cp config/reviewer.example.yaml config/reviewer.yaml` (provider, model,
   upstream `api_base`, toolset, pinned skill SHA).
2. Secrets under `~/.config/review-assistant`, mode `600`, outside the repository (D19):
   - `read-token`: read-only fine-grained token (contents, metadata, pull requests read)
   - `write.env`: one line `REVIEW_ASSISTANT_GH_TOKEN=<token>`
   - `inference-key`: the Helmcode API key, passed to the container as `REVIEW_ASSISTANT_INFERENCE_KEY`; the CLI
     pops it before any child process starts and hands the agent only the proxy sentinel
   - ADC: `~/.config/gcloud/application_default_credentials.json`; set the Vertex project ID in
     `deploy/hermes/config.yaml` (the committed file holds the placeholder; do not commit the real value). Only the
     Vertex route needs it: a `custom` provider with `api_base` reads no Google credential.
3. Host tools: `uv sync` creates `.venv` with `review-assistant-container`.
4. Build: `podman build -t review-assistant:dev .`
5. Isolation check (the agent's own view of what it can reach): `.venv/bin/review-assistant-container --check-isolation`
   exits 0.
6. Install the units: `mkdir -p ~/.config/systemd/user && cp deploy/review-assistant.service deploy/review-assistant.timer ~/.config/systemd/user/ && systemctl --user daemon-reload`
7. Hourly dry-run timer: `systemctl --user enable --now review-assistant.timer`

No openQA clone setup is needed: each run clones the repository shallow into its own scratch directory.

## Runs

- Dry-run is the default: reviews are printed, nothing is posted, no state is written.
- `review-assistant-container --post` posts reviews. `--limit N` caps new reviews per run (default `pilot_cap`).
  State records each posted review by head SHA, so a PR already reviewed at its current head is skipped.
- `--post` fails before any listing when `REVIEW_ASSISTANT_GH_TOKEN` is unset.
- The CLI takes both GitHub tokens and the inference key out of its own environment before Hermes starts and makes
  itself non-dumpable, so the agent cannot read them. The agent can read the ADC (accepted risk, D21); reviews carrying
  a token or credential pattern are refused before they are posted, and token values are redacted from log lines.

## OpenPlatform access

Deployment access details are intentionally kept out of this repository. Local operator notes hold the Rancher URL, cluster and project identifiers, and the kubeconfig path.

In the Rancher UI, use Cluster Explorer, then Workloads and Pods to reach the Deployment and its live container log. With `kubectl`:

```bash
kubectl --kubeconfig <kubeconfig> -n <namespace> logs -f deploy/review-assistant
```

The daemon logs warnings for reviews that are not recorded; successful dry-run reviews are printed to stdout.

## Model route

Switching the reviewer to another endpoint is a configuration change, not a code change:

- OpenAI-compatible endpoint: set `provider: custom`, `model: <id>` and `api_base: https://<host>/v1`, and provide
  `REVIEW_ASSISTANT_INFERENCE_KEY`. The loopback proxy binds `127.0.0.1` only, forwards `/v1/...` unchanged and adds
  the `Authorization` header.
- Gemini through Vertex: drop `api_base` and set `provider: google-vertex`, `model: google/gemini-3.8-flash`; keep the
  ADC mount and `CLOUDSDK_CONFIG` as provisioned for the Vertex route.

The endpoint must be reachable on TCP 443: `deploy/kubernetes/base/networkpolicy.yaml` allows DNS and `0.0.0.0/0:443`
minus the private ranges.

## Rollback

1. `systemctl --user disable --now review-assistant.timer`
2. Revoke both GitHub tokens and the Helmcode key, and rotate the ADC if a key leaked.
3. Delete `~/.local/state/review-assistant/state.json`.
4. D10: 3 or more AI-vs-human verdict conflicts stop the pilot.
