# OpenQA Model Review Assistant

Autonomous, model-based pull request review service for `os-autoinst/openQA`.

## Language

**Reviewer Daemon**:
A continuous background process that monitors pull requests via outbound conditional polling and executes reviews sequentially.
_Avoid_: Webhook listener, cron runner, polling bot

**Deduplication State**:
A persistent record mapping pull request identifiers and head commit SHAs to review statuses to prevent duplicate reviews on identical revisions.
_Avoid_: Cache, review database, state cache

**Advisory Review**:
A model-generated GitHub pull request review submitted with `COMMENT` event that provides feedback without counting towards mandatory human merge approvals.
_Avoid_: Automated approval, bot vote, merge check

**Skill Checkout**:
A local clone of `os-autoinst/openQA-skill` pinned to an immutable git commit hash, defining domain review rules and prompt playbooks.
_Avoid_: System prompt, prompt template, instructions repo
