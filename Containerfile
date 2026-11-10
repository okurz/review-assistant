# Base pinned by digest, resolved from registry.opensuse.org/opensuse/tumbleweed:latest on 2026-11-10.
FROM registry.opensuse.org/opensuse/tumbleweed@sha256:28dc49bdc6463892bc6346d08f7972eb71b090d8752b05ae827d0d07f2e84660

RUN zypper --non-interactive install --no-recommends git gh python313 python314 uv \
    && zypper clean --all

# Hermes v0.22.2 is installed from its source at the pinned commit with its uv.lock, which resolves for Python 3.14 only.
RUN git clone --no-checkout https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent \
    && git -C /opt/hermes-agent checkout --detach d923a5eb487a17c0a68801bba9ef0f56c81040a8 \
    && cd /opt/hermes-agent && uv sync --frozen --no-dev --python /usr/bin/python3.14

# Only package sources are copied: credentials live outside the repository and must not enter the image.
WORKDIR /opt/review-assistant
COPY pyproject.toml Readme.md ./
COPY review_assistant ./review_assistant
RUN uv venv --python /usr/bin/python3.13 /opt/venv && VIRTUAL_ENV=/opt/venv uv pip install .

ENV PATH=/opt/venv/bin:/opt/hermes-agent/.venv/bin:$PATH
ENTRYPOINT ["review-assistant"]
