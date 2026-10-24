# javis-uptime

External probe for [Javis](https://javis.yvjianta.com) and NeuroMem, run by GitHub Actions every
5 minutes (`.github/workflows/probe.yml` → `probe.py`). Part of the private `javis-infra` setup.

| Check | Healthy when |
|---|---|
| Javis login page | `https://javis.yvjianta.com/authelia/api/health` returns 200 |
| Javis 2FA gate | `https://javis.yvjianta.com/` returns 302 to `/authelia` (nothing is reachable without login) |
| NeuroMem | `https://yvjianta.com/healthz` returns 200 |
| TLS certificates | valid, and ≥ 14 days left on both hosts |
| Server heartbeat | the server's own watchdog reported "ok" within 90 minutes (`heartbeat` branch) |

A check fails only if three attempts 10 s apart all fail. Then an issue labelled `incident`
is opened and @-mentions the owner (GitHub notifies you). It is updated if the set of failing
checks changes, and is commented on and closed automatically on recovery.

Test the alert path: Actions → probe → Run workflow → tick "simulate failure". Run it again
without the tick to close the issue.

This repository is public only so that Actions minutes are free. It holds no secrets: just
public URLs and up/down results.
