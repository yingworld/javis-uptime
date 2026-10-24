#!/usr/bin/env python3
"""External probe for Javis + NeuroMem (javis-infra P0-4). Runs every 5 minutes on GitHub Actions.

One GitHub issue per incident (labelled `incident`, @-mentions the owner so a notification is
sent), a comment when the set of failing checks changes, and a comment + close on recovery.
A check fails only if 3 attempts 10 s apart all fail, so a single network blip stays quiet.
The job itself only fails when this script crashes (no e-mail flood during an outage).
"""
import json, os, socket, ssl, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

OWNER = "yingworld"
REPO = os.environ.get("GITHUB_REPOSITORY", "yingworld/javis-uptime")
CERT_MIN_DAYS = 14
HEARTBEAT_MAX_AGE_MIN = 90
TZ = ZoneInfo("Asia/Shanghai")
UA = "javis-uptime/1 (+https://github.com/yingworld/javis-uptime)"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http(url, timeout=15):
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.headers, time.monotonic() - t
    except urllib.error.HTTPError as e:  # 3xx (redirects disabled), 4xx, 5xx
        return e.code, e.headers, time.monotonic() - t


def status_check(url, want, location_contains=None):
    def run():
        code, headers, dt = http(url)
        if code != want:
            return False, f"HTTP {code}, expected {want}"
        loc = headers.get("Location") or ""
        if location_contains and location_contains not in loc:
            return False, f"redirects to {loc[:80]!r}, expected {location_contains!r}"
        return True, f"HTTP {code} in {dt * 1000:.0f} ms"
    return run


def cert_check(host):
    def run():
        ctx = ssl.create_default_context()  # also fails on an invalid / mismatched certificate
        with socket.create_connection((host, 443), timeout=15) as s:
            with ctx.wrap_socket(s, server_hostname=host) as t:
                not_after = t.getpeercert()["notAfter"]
        days = (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400
        return days >= CERT_MIN_DAYS, f"expires in {days:.0f} days"
    return run


def heartbeat_check():
    """Server-side watchdog report, pushed by the server to the `heartbeat` branch."""
    r = subprocess.run(["git", "fetch", "-q", "--depth", "1", "origin", "heartbeat"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        if os.environ.get("HEARTBEAT_REQUIRED") == "true":
            return False, "no heartbeat branch"
        return None, "not set up yet"
    hb = json.loads(subprocess.run(["git", "show", "FETCH_HEAD:heartbeat.json"],
                                   capture_output=True, text=True, check=True).stdout)
    age_min = (time.time() - hb["epoch"]) / 60
    if age_min > HEARTBEAT_MAX_AGE_MIN:
        return False, f"last heartbeat {age_min:.0f} min ago (server or its watchdog is down)"
    if not hb.get("ok"):
        return False, "server reports: " + "; ".join(hb.get("problems") or ["(no detail)"])[:400]
    return True, f"{age_min:.0f} min ago, all server-side checks ok"


CHECKS = [
    ("javis-login", "Javis login page (Authelia health)",
     status_check("https://javis.yvjianta.com/authelia/api/health", 200)),
    ("javis-gate", "Javis is behind the 2FA gate (/ → 302 /authelia)",
     status_check("https://javis.yvjianta.com/", 302, "/authelia")),
    ("neuromem", "NeuroMem (yvjianta.com/healthz)",
     status_check("https://yvjianta.com/healthz", 200)),
    ("cert-javis", "TLS certificate javis.yvjianta.com", cert_check("javis.yvjianta.com")),
    ("cert-neuromem", "TLS certificate yvjianta.com", cert_check("yvjianta.com")),
]


def attempt(fn, tries=3, gap=10):
    last = (False, "not run")
    for i in range(tries):
        try:
            last = fn()
        except Exception as e:  # timeouts, DNS, TLS errors
            last = (False, f"{type(e).__name__}: {e}"[:200])
        if last[0] is not False:
            return last
        if i < tries - 1:
            time.sleep(gap)
    return last


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def main():
    results = []
    for key, label, fn in CHECKS:
        ok, detail = attempt(fn)
        results.append((key, label, ok, detail))
    ok, detail = attempt(heartbeat_check, tries=2, gap=5)
    results.append(("server", "Server-side watchdog heartbeat", ok, detail))
    if os.environ.get("SIMULATE_FAILURE") == "true":
        results.append(("simulated", "Simulated failure (manual alert test)", False, "workflow_dispatch test"))

    now = datetime.now(timezone.utc)
    stamp = f"{now.astimezone(TZ):%Y-%m-%d %H:%M} (Asia/Shanghai)"
    rows = "\n".join(f"| {'✅' if ok else ('➖' if ok is None else '❌')} | {label} | {detail} |"
                     for _, label, ok, detail in results)
    table = f"| | Check | Result |\n|---|---|---|\n{rows}"
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{REPO}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    print(table)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"### Probe {stamp}\n\n{table}\n")

    failing = sorted(k for k, _, ok, _ in results if ok is False)
    marker = f"<!-- failing: {','.join(failing)} -->"
    gh("label", "create", "incident", "--repo", REPO, "--color", "d73a4a",
       "--description", "Opened and closed automatically by the probe", "--force")
    issues = json.loads(gh("issue", "list", "--repo", REPO, "--label", "incident", "--state", "open",
                           "--json", "number,title,body,createdAt,comments"))
    if failing:
        names = ", ".join(label for k, label, ok, _ in results if ok is False)
        title = f"🔴 Javis 故障：{names}"[:200]
        body = (f"@{OWNER} 外部探活发现故障（每 5 分钟检查一次，连续 3 次失败才报警）。\n\n"
                f"{table}\n\n时间：{stamp} · [运行记录]({run_url})\n\n"
                f"恢复后本 issue 会自动关闭。处理办法见 javis-infra 的 server/README.md。\n{marker}")
        if not issues:
            gh("issue", "create", "--repo", REPO, "--title", title, "--body", body, "--label", "incident")
            print("opened incident issue")
        else:
            n = issues[0]["number"]
            texts = [issues[0]["body"]] + [c["body"] for c in issues[0]["comments"]]
            last = next((t for t in reversed(texts) if "<!-- failing:" in t), "")
            if marker not in last:
                gh("issue", "comment", str(n), "--repo", REPO, "--body",
                   f"@{OWNER} 故障范围有变化：\n\n{table}\n\n{stamp} · [运行记录]({run_url})\n{marker}")
                gh("issue", "edit", str(n), "--repo", REPO, "--title", title)
                print(f"updated incident #{n}")
    else:
        for it in issues:
            started = datetime.fromisoformat(it["createdAt"].replace("Z", "+00:00"))
            mins = (now - started).total_seconds() / 60
            gh("issue", "comment", str(it["number"]), "--repo", REPO, "--body",
               f"✅ @{OWNER} 已恢复（约 {mins:.0f} 分钟）。\n\n{table}\n\n{stamp} · [运行记录]({run_url})")
            gh("issue", "close", str(it["number"]), "--repo", REPO)
            print(f"closed incident #{it['number']}")

    # Keep the schedule alive: GitHub disables scheduled workflows in repos without activity
    # for 60 days; re-enabling through the API resets that clock. Once a day is plenty.
    if now.hour == 0 and now.minute < 10:
        r = subprocess.run(["gh", "api", "-X", "PUT", f"repos/{REPO}/actions/workflows/probe.yml/enable"],
                           capture_output=True, text=True)
        print("keepalive:", "ok" if r.returncode == 0 else r.stderr.strip()[:200])


if __name__ == "__main__":
    main()
