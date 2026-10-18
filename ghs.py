#!/usr/bin/env python3
"""GitHub open-source hunter.

Search public GitHub repos and evaluate whether they are actually usable
locally (activity, license, install hints, Windows compatibility).

Stdlib only. No third-party deps.

Subcommands:
  search    Search repositories, printed as compact ranked cards.
  repo      Inspect one repo: stats, README digest, install hints.
  token     set / show / clear a stored GitHub token.

Auth resolution order (first hit wins):
  --token  >  $GITHUB_TOKEN  >  $GH_TOKEN  >  stored token file
Anonymous search works but is limited to ~10 requests/min.
"""

import argparse
import base64
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"


def _config_dir():
    """Per-user config dir, cross-platform, no third-party deps."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "gh-search"


TOKEN_FILE = _config_dir() / "token"
UA = "gh-search-cli/1.0"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- http layer

class RateLimited(Exception):
    pass


def load_token(cli_token=None):
    if cli_token:
        return cli_token.strip()
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()
    if TOKEN_FILE.is_file():
        try:
            txt = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except Exception:
            pass
    return None


def api_get(path, params=None, token=None, accept="application/vnd.github+json"):
    """GET an API path. Returns (parsed_json_or_text, headers)."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA, "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            hdrs = dict(resp.headers)
            ctype = hdrs.get("Content-Type", "")
            if "json" in ctype:
                return json.loads(raw.decode("utf-8", "replace")), hdrs
            return raw.decode("utf-8", "replace"), hdrs
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        if e.code in (403, 429):
            remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
            reset = e.headers.get("X-RateLimit-Reset") if e.headers else None
            wait = ""
            if reset:
                try:
                    wait = " resets in %ds" % max(0, int(reset) - int(time.time()))
                except Exception:
                    pass
            if remaining == "0" or "rate limit" in body.lower():
                raise RateLimited(
                    "GitHub rate limit hit (HTTP %d)%s. %s"
                    % (e.code, wait, "Add a token for 30x the limit." if not token else "")
                )
        raise RuntimeError("HTTP %d on %s: %s" % (e.code, path, body.strip() or e.reason))
    except urllib.error.URLError as e:
        raise RuntimeError(
            "Network unreachable (%s). GitHub API may be blocked on this network; "
            "try again, or fall back to a plain web search." % e.reason
        )


# ------------------------------------------------------------------- scoring

def days_since(iso):
    if not iso:
        return 9999
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 9999


def health(repo):
    """Return (score 0-100, list of verdict tags). Cheap heuristic, not gospel."""
    stars = repo.get("stargazers_count") or 0
    idle = days_since(repo.get("pushed_at") or repo.get("updated_at"))
    tags = []

    score = min(45.0, math.log10(max(stars, 1)) * 15.0)      # popularity, capped
    if idle <= 90:
        score += 30
        tags.append("active")
    elif idle <= 365:
        score += 18
        tags.append("low-activity" if idle > 180 else "moderate")
    elif idle <= 730:
        score += 6
        tags.append("semi-dormant")
    else:
        tags.append("likely-dormant")

    if repo.get("archived"):
        score -= 25
        tags.append("archived")
    lic = (repo.get("license") or {}).get("spdx_id")
    if lic and lic not in ("NOASSERTION", None):
        score += 12
    else:
        tags.append("no-license")
    if repo.get("has_issues"):
        open_issues = repo.get("open_issues_count") or 0
        if stars > 500 and open_issues > stars * 1.5:
            tags.append("issue-pileup")
    if repo.get("fork"):
        score -= 15
        tags.append("fork")

    return max(0.0, min(100.0, score)), tags


# Optional, caller-supplied deny list. Empty by default: this tool applies no
# editorial policy of its own. Set GHS_DENY_TOPICS="word1,word2" (matched
# case-insensitively against topics + description) if you want to mute certain
# kinds of noise in your own searches.
DENY_TOPICS_ENV = "GHS_DENY_TOPICS"


def _deny_list():
    raw = os.environ.get(DENY_TOPICS_ENV, "").strip()
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def is_blocked(repo):
    """True only when the caller configured a deny list and this repo matches it."""
    deny = _deny_list()
    if not deny:
        return False
    blob = " ".join(repo.get("topics") or []).lower() + " " + (repo.get("description") or "").lower()
    return any(p in blob for p in deny)


def fmt_stars(n):
    n = n or 0
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def ago(iso):
    d = days_since(iso)
    if d >= 9999:
        return "?"
    if d <= 1:
        return "today"
    if d < 30:
        return "%dd" % d
    if d < 365:
        return "%dmo" % (d // 30)
    return "%.1fy" % (d / 365.0)


def human_repo(repo, idx=None, score_override=None, extra_tags=None):
    score, tags = health(repo)
    if score_override is not None:
        score = score_override
    for t in (extra_tags or []):
        if t not in tags:
            tags.append(t)
    lic = (repo.get("license") or {}).get("spdx_id") or "no-license"
    head = "%s%s  %s  %s  %s  pushed:%s  score:%.0f" % (
        ("[%d] " % idx) if idx else "",
        repo.get("full_name", "?"),
        fmt_stars(repo.get("stargazers_count")),
        repo.get("language") or "-",
        lic,
        ago(repo.get("pushed_at") or repo.get("updated_at")),
        score,
    )
    lines = [head]
    desc = (repo.get("description") or "").strip()
    if desc:
        lines.append("    " + (desc[:220] + ("..." if len(desc) > 220 else "")))
    if repo.get("topics"):
        lines.append("    topics: " + ", ".join(repo["topics"][:8]))
    lines.append("    " + " / ".join(tags))
    if repo.get("html_url"):
        lines.append("    " + repo["html_url"])
    return "\n".join(lines)


# ------------------------------------------------------------- readme digest

BADGE = re.compile(r"^\s*(\[!\[|!\[|<img|<a |<p align|</?div|</?h[1-6]|---|===|\|)")
INSTALL_PAT = re.compile(
    r"^\s*\$?\s*(?:sudo\s+)?("
    r"pip3?\s+install\s+[^\s#]+|pipx\s+install\s+[^\s#]+|uv\s+(?:tool|pip)\s+install\s+[^\s#]+|"
    r"npm\s+(?:i|install)\s+-g\s+[^\s#]+|npm\s+(?:i|install)\s+[^\s#]+|"
    r"pnpm\s+(?:add|i)\s+-g\s+[^\s#]+|yarn\s+(?:global\s+)?add\s+[^\s#]+|"
    r"npx\s+[^\s#]+|bunx\s+[^\s#]+|"
    r"brew\s+install\s+[^\s#]+|winget\s+install\s+[^\s#]+|scoop\s+install\s+[^\s#]+|choco\s+install\s+[^\s#]+|"
    r"cargo\s+install\s+[^\s#]+|go\s+install\s+[^\s#]+|"
    r"docker\s+(?:run|pull)\s+[^\s#]+|"
    r"git\s+clone\s+[^\s#]+"
    r")",
    re.IGNORECASE,
)


def digest_readme(text, budget=1800):
    if not text:
        return "    (no README)", []
    lines, out, used = text.splitlines(), [], 0
    for ln in lines:
        s = ln.rstrip()
        if not s.strip() or BADGE.match(s):
            continue
        if s.strip().startswith("```"):
            continue
        if used + len(s) > budget:
            break
        out.append(s)
        used += len(s) + 1
    installs, seen = [], set()
    for ln in lines:
        m = INSTALL_PAT.match(ln.replace("`", ""))
        if m:
            cmd = re.sub(r"\s+", " ", m.group(1)).strip()
            key = cmd.lower()
            if key not in seen:
                seen.add(key)
                installs.append(cmd)
        if len(installs) >= 6:
            break
    body = "\n".join("    " + s for s in out) if out else "    (README empty)"
    return body, installs


DEAD_PAT = re.compile(
    r"no longer (actively )?maintained|no longer maintained|unmaintained|"
    r"not (actively )?maintained|discontinued|abandoned|"
    r"不再维护|停止维护|已停止更新|作者已弃坑",
    re.IGNORECASE,
)


def platform_flags(repo, readme_text):
    """Cheap hints about whether this runs on the local Windows machine."""
    flags = []
    txt = (readme_text or "").lower()
    if DEAD_PAT.search(readme_text or ""):
        flags.append("WARN README says unmaintained (more reliable than push date)")
    if repo.get("language") in ("Python", "JavaScript", "TypeScript", "Rust", "Go", "Java"):
        flags.append("cross-platform language")
    if re.search(r"\bwindows\b", txt):
        flags.append("README mentions Windows")
    if re.search(r"only (works|supported) on (macos|linux)|macos[- ]only|linux[- ]only", txt):
        flags.append("WARN possibly macOS/Linux only")
    if re.search(r"\bwinget\b|\bscoop\b|\.exe\b|releases/download", txt):
        flags.append("has Windows install path")
    return flags


# --------------------------------------------------------------- subcommands

STOPWORDS = {"a", "an", "the", "to", "for", "of", "in", "on", "and", "or", "with",
             "from", "by", "how", "my", "is", "it", "that", "this"}


def build_variants(query, quals):
    """Progressive relaxations, widest last. GitHub search is AND-based:
    every extra token cuts the result set, so zero-hit queries must shed tokens."""
    words = query.split()
    core = [w for w in words if w.lower() not in STOPWORDS] or words
    out = []

    def add(q_words, q_quals, label):
        q = " ".join(q_words + q_quals).strip()
        if q and q not in [v[0] for v in out]:
            out.append((q, label))

    add(words, quals, None)
    if not quals:
        # nothing to relax but the words themselves
        pass
    else:
        # 1. drop time filter (usually the most damaging)
        q2 = [x for x in quals if not x.startswith("pushed:")]
        add(words, q2, "dropped pushed: filter")
        # 2. drop archived/fork constraints too
        q3 = [x for x in q2 if not x.startswith(("archived:", "fork:"))]
        add(words, q3, "dropped pushed:/archived:/fork: filters")
        # 3. drop star floor
        q4 = [x for x in q3 if not x.startswith("stars:")]
        add(words, q4, "dropped star floor")
        base_quals = q4
    if not quals:
        base_quals = []
    # 4. trim tokens
    if len(core) > 3:
        add(core[:3], base_quals, "kept first 3 keywords")
    if len(core) > 2:
        add(core[:2], base_quals, "kept first 2 keywords")
    if len(core) > 1:
        add(core[:1], base_quals, "kept 1 keyword")
    return out


def cmd_search(args):
    token = load_token(args.token)
    q = args.query
    quals = []
    if args.min_stars:
        quals.append("stars:>=%d" % args.min_stars)
    if args.pushed_after:
        quals.append("pushed:>%s" % args.pushed_after)
    if args.lang:
        quals.append("language:%s" % args.lang)
    if args.topic:
        quals.append("topic:%s" % args.topic)
    if args.no_archived:
        quals.append("archived:false")
    if args.not_fork:
        quals.append("fork:false")

    variants = build_variants(q, quals) if args.auto_relax else [(" ".join([q] + quals).strip(), None)]
    # language/topic express intent, never relax them away
    keep = [x for x in quals if x.startswith(("language:", "topic:"))]

    items, total, full, relaxed, tried = [], 0, variants[0][0], None, []
    for i, (cand, label) in enumerate(variants):
        if i > 0:
            cand = " ".join([cand] + [k for k in keep if k not in cand]).strip()
        if cand in tried:
            continue
        tried.append(cand)
        if len(tried) > 4:           # stay polite with the anonymous rate limit
            break
        params = {
            "q": cand,
            "sort": args.sort,
            "order": "desc",
            "per_page": max(1, min(args.limit, 30)),
        }
        data, _ = api_get("/search/repositories", params, token)
        raw = data.get("items") or []
        got = [r for r in raw if not is_blocked(r)]
        blocked = len(raw) - len(got)
        if blocked and not args.json:
            print("[filtered %d political/unrelated hit(s)]" % blocked)
        if got:
            items, total, full, relaxed = got, data.get("total_count", 0) - blocked, cand, label
            break

    if args.json:
        slim = []
        for r in items:
            score, tags = health(r)
            slim.append({
                "full_name": r.get("full_name"),
                "html_url": r.get("html_url"),
                "stars": r.get("stargazers_count"),
                "language": r.get("language"),
                "license": (r.get("license") or {}).get("spdx_id"),
                "pushed_at": r.get("pushed_at"),
                "archived": r.get("archived"),
                "description": r.get("description"),
                "topics": r.get("topics"),
                "score": round(score, 1),
                "tags": tags,
            })
        print(json.dumps({
            "ok": True, "query": full, "original_query": variants[0][0],
            "relaxed": relaxed, "tried": tried,
            "total": total, "authenticated": bool(token), "items": slim,
        }, ensure_ascii=False, indent=2))
        return 0

    if not items:
        print("NO_RESULTS after %d attempts:" % len(tried))
        for t in tried:
            print("    - %s" % t)
        print("Hint: translate the task to different English synonyms (see")
        print("      references/keyword-mapping.md), or search for the underlying")
        print("      library rather than the whole feature.")
        return 0

    auth = "token" if token else "anonymous(10 req/min)"
    print("query: %s" % full)
    if relaxed:
        print("note: original query returned nothing; relaxed by -> %s" % relaxed)
    print("total: %s   showing: %d   auth: %s" % (total, len(items), auth))
    print("")
    ranked = sorted(items, key=lambda r: health(r)[0], reverse=True)
    for i, r in enumerate(ranked, 1):
        print(human_repo(r, i))
        print("")
    top = health(ranked[0])[0]
    if top < 60:
        print("LOW_CONFIDENCE: best candidate scores only %.0f/100." % top)
        print("Likely the keywords miss the ecosystem term. Try the package/module")
        print("name people actually use (e.g. 'pdf2docx', 'yt-dlp'), or an 'awesome X' list.")
        print("")
    print("Next: python %s repo <owner/name>" % os.path.basename(__file__))
    return 0


def cmd_repo(args):
    token = load_token(args.token)
    name = args.name.strip().strip("/")
    if name.startswith("http"):
        parts = [p for p in urllib.parse.urlparse(name).path.split("/") if p]
        if len(parts) >= 2:
            name = parts[0] + "/" + parts[1]
    if "/" not in name:
        # a bare package name like "pdf2docx" -> resolve via search
        data, _ = api_get("/search/repositories",
                          {"q": "%s in:name" % name, "sort": "stars",
                           "order": "desc", "per_page": 5}, token)
        hits = [r for r in (data.get("items") or []) if not is_blocked(r)]
        exact = [r for r in hits if str(r.get("name", "")).lower() == name.lower()]
        pick = (exact or hits or [None])[0]
        if not pick:
            print("ERROR: no repo named %r found. Pass owner/name or a full URL." % name)
            return 1
        name = pick["full_name"]
        if not args.json:
            print("[resolved %r -> %s]" % (args.name.strip(), name))
    repo, _ = api_get("/repos/" + name, None, token)

    readme_text = ""
    try:
        rm, _ = api_get("/repos/%s/readme" % name, None, token)
        if isinstance(rm, dict) and rm.get("content"):
            readme_text = base64.b64decode(rm["content"]).decode("utf-8", "replace")
    except RateLimited:
        raise
    except Exception:
        pass

    try:
        rel, _ = api_get("/repos/%s/releases/latest" % name, None, token)
        latest = "%s (%s)" % (rel.get("tag_name"), (rel.get("published_at") or "")[:10]) if isinstance(rel, dict) else None
    except Exception:
        latest = None

    score, tags = health(repo)
    body, installs = digest_readme(readme_text)
    flags = platform_flags(repo, readme_text)
    if any(f.startswith("WARN README says unmaintained") for f in flags):
        # an explicit "unmaintained" beats a recent commit: authors push docs fixes
        score = min(score, 45.0)
        if "self-declared-unmaintained" not in tags:
            tags.append("self-declared-unmaintained")

    if args.json:
        print(json.dumps({
            "ok": True,
            "full_name": repo.get("full_name"),
            "html_url": repo.get("html_url"),
            "description": repo.get("description"),
            "stars": repo.get("stargazers_count"),
            "forks": repo.get("forks_count"),
            "open_issues": repo.get("open_issues_count"),
            "language": repo.get("language"),
            "license": (repo.get("license") or {}).get("spdx_id"),
            "pushed_at": repo.get("pushed_at"),
            "created_at": repo.get("created_at"),
            "archived": repo.get("archived"),
            "default_branch": repo.get("default_branch"),
            "latest_release": latest,
            "score": round(score, 1),
            "tags": tags,
            "platform_flags": flags,
            "install_hints": installs,
            "topics": repo.get("topics"),
            "readme_excerpt": readme_text[:4000],
        }, ensure_ascii=False, indent=2))
        return 0

    print(human_repo(repo, None, score, tags))
    print("    forks:%s  issues:%s  created:%s  branch:%s" % (
        repo.get("forks_count"), repo.get("open_issues_count"),
        (repo.get("created_at") or "")[:10], repo.get("default_branch")))
    if latest:
        print("    latest release: " + latest)
    if flags:
        print("    platform: " + " / ".join(flags))
    print("")
    print("== install hints ==")
    print("\n".join("    " + c for c in installs) if installs else "    (none detected - read README)")
    print("")
    print("== README excerpt ==")
    print(body)
    return 0


def cmd_token(args):
    if args.token_action == "set":
        val = sys.stdin.read().strip() if not args.value else args.value.strip()
        if not val:
            print("ERROR: empty token")
            return 1
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(val, encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except Exception:
            pass
        print("saved -> %s" % TOKEN_FILE)
        try:
            me, _ = api_get("/user", None, val)
            print("verified as: %s" % me.get("login"))
        except Exception as e:
            print("WARN: token stored but /user failed: %s" % e)
        return 0
    if args.token_action == "clear":
        if TOKEN_FILE.is_file():
            TOKEN_FILE.unlink()
            print("cleared")
        else:
            print("nothing stored")
        return 0
    tok = load_token(None)
    if not tok:
        print("no token. anonymous mode (10 search req/min).")
        print("to add one: python %s token set" % os.path.basename(__file__))
        return 0
    try:
        me, hdrs = api_get("/user", None, tok)
        print("token: ****%s  user: %s  scope: %s" % (
            tok[-4:], me.get("login"), hdrs.get("X-OAuth-Scopes", "(fine-grained)")))
    except Exception as e:
        print("token present but invalid: %s" % e)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="ghs",
        description="Search and vet public GitHub repos for reusable solutions.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search repositories")
    s.add_argument("query", help="GitHub search query, English keywords preferred")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--sort", default="stars",
                   choices=["stars", "forks", "updated", "best-match", "help-wanted-issues"])
    s.add_argument("--min-stars", type=int, default=0)
    s.add_argument("--pushed-after", default="", help="YYYY-MM-DD")
    s.add_argument("--lang", default="")
    s.add_argument("--topic", default="")
    s.add_argument("--no-archived", action="store_true", default=True)
    s.add_argument("--allow-archived", dest="no_archived", action="store_false")
    s.add_argument("--not-fork", action="store_true", default=True)
    s.add_argument("--allow-fork", dest="not_fork", action="store_false")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-relax", dest="auto_relax", action="store_false", default=True,
                   help="disable automatic query relaxation on zero results")
    s.add_argument("--token", default=None)
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("repo", help="inspect one repo")
    r.add_argument("name", help="owner/name or full URL")
    r.add_argument("--json", action="store_true")
    r.add_argument("--token", default=None)
    r.set_defaults(func=cmd_repo)

    t = sub.add_parser("token", help="manage stored token")
    t.add_argument("token_action", nargs="?", default="show", choices=["set", "show", "clear"])
    t.add_argument("value", nargs="?", default=None)
    t.set_defaults(func=cmd_token)

    args = ap.parse_args()
    try:
        return args.func(args)
    except RateLimited as e:
        print("RATE_LIMITED: %s" % e)
        return 2
    except RuntimeError as e:
        print("ERROR: %s" % e)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
