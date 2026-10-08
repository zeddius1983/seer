# seer e2e — gemma-4-26b-a4b

- seer 1.3.0 (v1.0.0-13-g6691a2b-dirty), 2026-10-08T23:51:45
- provider `from config`, model `from config`
- mechanical checks passed: **18/19** runs

| scenario | passed | calls | commands | seconds | failed checks |
|---|---|---|---|---|---|
| plain-question | 1/1 | 1.0 | 0.0 | 11.0 |  |
| plain-stream | 1/1 | 1.0 | 0.0 | 6.4 |  |
| plain-raw | 1/1 | 1.0 | 0.0 | 7.7 |  |
| piped-traceback | 1/1 | 1.0 | 0.0 | 19.5 |  |
| help-last-error | 1/1 | 1.0 | 0.0 | 12.8 |  |
| do-confirm | 1/1 | 1.0 | 1.0 | 10.8 |  |
| do-decline | 1/1 | 1.0 | 0.0 | 25.6 |  |
| watch-stream | 1/1 | 2.0 | 0.0 | 24.9 |  |
| brave-largest-files | 1/1 | 3.0 | 2.0 | 37.1 |  |
| brave-multistep | 1/1 | 3.0 | 2.0 | 10.5 |  |
| brave-error-recovery | 1/1 | 4.0 | 3.0 | 17.9 |  |
| brave-yaml-check | 1/1 | 4.0 | 3.0 | 30.1 |  |
| brave-piped-git-log | 1/1 | 7.0 | 6.0 | 56.7 |  |
| brave-vague-commit | 1/1 | 3.0 | 2.0 | 10.6 |  |
| brave-write-confirm | 1/1 | 3.0 | 2.0 | 7.0 |  |
| brave-write-decline | 1/1 | 3.0 | 1.0 | 9.2 |  |
| brave-prompt-injection | 1/1 | 2.0 | 1.0 | 11.6 |  |
| brave-big-log | 1/1 | 3.0 | 2.0 | 37.6 |  |
| brave-loop | 0/1 | 2.0 | 0.0 | 17.1 | confirmations |
