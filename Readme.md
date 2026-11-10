# review-assistant

Automated, model-based pull request review for `os-autoinst/openQA`.

The assistant reviews open pull requests with the `os-autoinst/openQA-skill` agent skill (pinned to a commit) and a Haiku 5.5 model, and posts the result as a `COMMENT` review from a separate, non-personal GitHub account. It is intended as one advisory review among the two approvals required by the project, to reduce human review load. It never approves, requests changes, or merges.

Status: pilot planning. See `tasks/001_210027_automated_model_review.md` for the plan and `docs/github-review-account-setup.md` for the account and token setup.

Tracker: https://progress.opensuse.org/issues/210027
