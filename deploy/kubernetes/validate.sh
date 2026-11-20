#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

KUSTOMIZE=${KUSTOMIZE:-kustomize}
KUBECONFORM=${KUBECONFORM:-kubeconform}
OVERLAYS=(
    deploy/kubernetes/overlays/openplatform
    deploy/kubernetes/overlays/openplatform-dry-run
)
STANDALONE=(
    deploy/kubernetes/base/namespace.yaml
    deploy/kubernetes/job-dry-run/job.yaml
    deploy/kubernetes/secret-template.yaml
)
SECRET_PATTERNS='ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|"private_key_id"|"refresh_token"|ya29\.|"type": "service_account"'

for tool in "$KUSTOMIZE" "$KUBECONFORM"; do
    command -v "$tool" >/dev/null || {
        echo "missing required tool: $tool" >&2
        exit 1
    }
done

rendered=$(mktemp --suffix=.yaml)
trap 'rm -f "$rendered"' EXIT

for overlay in "${OVERLAYS[@]}"; do
    "$KUSTOMIZE" build "$overlay" >"$rendered"

    if grep -q '^kind: Secret$' "$rendered"; then
        echo "rendered overlay contains a Secret; secrets must be provisioned out-of-band" >&2
        exit 1
    fi

    "$KUBECONFORM" -strict -summary "$rendered"

    if grep -nE "$SECRET_PATTERNS" "$rendered"; then
        echo "credential-like value found in rendered manifests" >&2
        exit 1
    fi
done

"$KUBECONFORM" -strict -summary "${STANDALONE[@]}"

if grep -nE "$SECRET_PATTERNS" "${STANDALONE[@]}" deploy/kubernetes/base/*.yaml deploy/kubernetes/job-dry-run/*.yaml deploy/kubernetes/overlays/*/*.yaml; then
    echo "credential-like value found in manifests" >&2
    exit 1
fi

if grep -vE '^(#|$|apiVersion:|kind:|metadata:|  name:|  namespace:|type:|stringData:|  [a-z0-9.-]+: REPLACE_ME$)' deploy/kubernetes/secret-template.yaml; then
    echo "secret-template.yaml may contain non-placeholder values" >&2
    exit 1
fi

echo "kubernetes manifests valid"
