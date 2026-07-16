# Notes for Claude

## Push schedule

Only push commits to the remote once per day, at 2am. Do not push after every
commit/batch of work — commit locally as normal, but hold pushes until the
2am window.

**Why:** Frequent pushes trigger GitHub Actions runs, and this project wants
to keep Actions usage/minutes low.
