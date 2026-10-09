"""Tickets kept in a tracker (Linear now; Jira Cloud and Jira Server/Data Center plug into the same shape next).

One contract per tracker: fetch(settings, ticket, token) -> {"id", "key", "title", "body", "url", "updated_at"} or raises
TrackerError with plain words. Read-only, a fixed HTTPS endpoint built here (never taken from a copy or a ticket), no
redirects followed, bounded time and size, the token sent only to that endpoint and never printed. The copy itself is
written and checked like a GitHub issue copy: the gate reads only the copy; only CI's own comparison can call it verified.
"""
import json, os, re, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

FORMAT = "1"                 # how a ticket becomes the copy's text; bump it if that ever changes, so old copies show "changed"
MAX_BYTES = 1_000_000        # an answer bigger than this isn't one ticket
MAX_BODY = 256_000
TIMEOUT = 20
TICKET = re.compile(r"[A-Z][A-Z0-9]{0,9}-[0-9]{1,9}")
WORKSPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LABEL = {"linear": "Linear"}
TOKEN_ENV = {"linear": "STORY_GATE_LINEAR_KEY"}          # the CI secret (read-only key)
LOCAL_ENV = {"linear": ("STORY_GATE_LINEAR_KEY", "LINEAR_API_KEY")}  # on your own computer, your own key
NOTE = "<!-- story-gate snapshot: don't edit by hand. Run `story-gate spec-pull %s %s` again to update it. -->"


class TrackerError(Exception):
    """Plain-English reason a ticket couldn't be read. Never carries the token."""


class NotFound(TrackerError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could carry the token elsewhere: never follow one
        return None


def post_json(url, headers, payload, tries=2, sleep=time.sleep):
    """(HTTP status, parsed JSON or None). Honours HTTPS_PROXY and the system's CA certificates (SSL_CERT_FILE), verifies
    TLS, follows no redirect, reads at most MAX_BYTES, waits at most TIMEOUT per try and retries once on 429/5xx."""
    if not url.startswith("https://") and not os.environ.get("STORY_GATE_TEST_HTTP"):
        raise TrackerError("refusing a non-HTTPS address")
    opener = urllib.request.build_opener(_NoRedirect)
    data = json.dumps(payload).encode("utf-8")
    hdrs = dict(headers, **{"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "story-gate"})
    host = url.split("/")[2]
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="POST", headers=hdrs)
        try:
            with opener.open(req, timeout=TIMEOUT) as r:
                raw = r.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise TrackerError("%s's answer was bigger than %d bytes" % (host, MAX_BYTES))
                try:
                    return r.status, json.loads(raw.decode("utf-8"))
                except ValueError:
                    return r.status, None
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt + 1 < tries:
                ra = str(e.headers.get("Retry-After") or "")
                sleep(min(int(ra), 10) if ra.isdigit() else 2)
                continue
            return e.code, None
        except TrackerError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:  # timeouts, DNS, TLS, refused
            if attempt + 1 < tries:
                sleep(1)
                continue
            raise TrackerError("couldn't reach %s (%s)" % (host, type(e).__name__))
    return 0, None


# ---------------------------------------------------------------- Linear
LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_QUERY = ("query StoryGateTicket($id: String!) { organization { urlKey } "
                "issue(id: $id) { id identifier title description url updatedAt } }")


def linear_fetch(settings, ticket, token, post=None):
    ws = settings.get("workspace")
    post = post or post_json
    st, data = post(LINEAR_URL, {"Authorization": token}, {"query": LINEAR_QUERY, "variables": {"id": ticket}})
    if st in (401, 403):
        raise TrackerError("Linear refused the key (HTTP %s)" % st)
    if st != 200 or not isinstance(data, dict):
        raise TrackerError("Linear answered HTTP %s" % st)
    d = data.get("data") if isinstance(data.get("data"), dict) else {}
    errs = data.get("errors")
    if errs:
        text = " ".join(str(e.get("message", "")) for e in errs if isinstance(e, dict)).lower()
        if "not found" in text and d.get("issue") is None:
            raise NotFound("%s wasn't found in Linear (or this key can't see it)" % ticket)
        if "authentication" in text or "forbidden" in text:
            raise TrackerError("Linear refused the key")
        raise TrackerError("Linear answered with an error")
    org = (d.get("organization") or {}).get("urlKey") if isinstance(d.get("organization"), dict) else None
    if org != ws:
        raise TrackerError("this key belongs to the Linear workspace '%s', not '%s' (trackers.linear.workspace)"
                           % (str(org)[:60], ws))
    iss = d.get("issue")
    if iss is None:
        raise NotFound("%s wasn't found in Linear (or this key can't see it)" % ticket)
    if not isinstance(iss, dict):
        raise TrackerError("Linear's answer isn't a ticket")
    for k in ("id", "identifier", "title", "url", "updatedAt"):
        if not isinstance(iss.get(k), str):
            raise TrackerError("Linear's answer has no '%s'" % k)
    body = iss.get("description")
    if body is not None and not isinstance(body, str):
        raise TrackerError("Linear's answer has an unreadable description")
    key = iss["identifier"]
    if not TICKET.fullmatch(key) or not UUID.fullmatch(iss["id"]):
        raise TrackerError("Linear's answer has an unexpected ticket id")
    if not iss["url"].startswith("https://linear.app/%s/issue/%s" % (ws, key)):
        raise TrackerError("Linear's answer points to another workspace")
    return {"id": iss["id"], "key": key, "title": iss["title"], "body": body or "", "updated_at": iss["updatedAt"][:40],
            "url": "https://linear.app/%s/issue/%s" % (ws, key)}


FETCH = {"linear": linear_fetch}


def ticket_url(kind, settings, key):
    if kind == "linear":
        return "https://linear.app/%s/issue/%s" % (settings.get("workspace"), key)
    raise TrackerError("unknown tracker %s" % kind)


def parse_ticket_ref(ref):
    """('linear' or None, 'ENG-12', workspace or None) for 'ENG-12' or a Linear issue URL; None if it isn't a ticket."""
    ref = (ref or "").strip()
    if TICKET.fullmatch(ref):
        return None, ref, None
    m = re.fullmatch(r"https://linear\.app/([a-z0-9-]{1,48})/issue/([A-Z][A-Z0-9]{0,9}-[0-9]{1,9})(?:/[^\s]*)?", ref)
    if m:
        return "linear", m.group(2), m.group(1)
    return None


# ---------------------------------------------------------------- the copy
def clean(s):
    return str(s or "").encode("utf-8", "replace").decode("utf-8")


def one_line(s, n=300):
    return re.sub(r"\s+", " ", clean(s)).strip()[:n]


def content(sid, key, title, body):
    """The text part of a ticket copy. CI rebuilds it from the live ticket the same way, so 'unchanged' means the same text."""
    return "%s\n\n# %s\n\n%s\n" % (NOTE % (sid, key), one_line(title) or "Ticket %s" % key,
                                   clean(body).replace("\r\n", "\n").replace("\r", "\n").rstrip())


def snapshot_text(gate, sid, kind, settings, key, meta, title, body):
    text = content(sid, key, title, body)
    fields = {k: one_line(meta[k]) for k in gate.TRACKER_KEYS}
    fields["content_sha256"] = gate.snapshot_hash(fields, text, gate.TRACKER_KEYS)
    return "---\n%s\n---\n%s" % ("\n".join("%s: %s" % kv for kv in fields.items()), text)


def local_token(kind):
    for name in LOCAL_ENV[kind]:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return ""


def public_repo_refused(gate, settings):
    """A plain-English refusal when copying private ticket text here could publish it, else None."""
    if settings.get("allow_in_public_repo") is True:
        return None
    own = gate.origin_repo()
    if not own:
        return "this repository's 'origin' isn't on github.com, so story-gate can't confirm it's private"
    import sg_github as G
    st, data, _ = G.call("GET", "/repos/%s" % own, G.human_token())
    if st != 200 or not isinstance(data, dict) or not isinstance(data.get("private"), bool):
        return "story-gate couldn't check whether %s is private (HTTP %s); sign in with `gh auth login`" % (own, st)
    if not data["private"]:
        return ("%s is public, so copying a ticket into it would publish the ticket. If that's intended, set "
                "\"allow_in_public_repo\": true for this tracker in .story-gate/config.json (reviewers see it as a weaker rule)" % own)
    return None


def cli(gate, sid, ref_arg, kv, sd, write_and_link):
    """spec-pull for a tracker ticket. `write_and_link(text, out, key)` is spec-pull's own safe write and story.md link."""
    parsed = parse_ticket_ref(ref_arg)
    trackers = gate.cfg().get("trackers") or {}
    kind, key, ws = parsed
    if kind is None:
        set_up = [k for k in FETCH if isinstance(trackers.get(k), dict)]
        if not set_up:
            sys.exit("story-gate: %s looks like a Linear or Jira ticket, but no tracker is set up. Add it to "
                     ".story-gate/config.json, e.g. \"trackers\": {\"linear\": {\"workspace\": \"your-workspace\"}} "
                     "(see the guide, 'Specs in Linear')." % key)
        kind = set_up[0]
    settings = trackers.get(kind)
    if not isinstance(settings, dict):
        sys.exit("story-gate: %s isn't set up in .story-gate/config.json (trackers.%s)." % (LABEL[kind], kind))
    if ws and ws != settings.get("workspace"):
        sys.exit("story-gate: that link is in the Linear workspace '%s', but this repository is set up for '%s'."
                 % (ws, settings.get("workspace")))
    why = public_repo_refused(gate, settings)
    if why:
        sys.exit("story-gate: nothing was copied: " + why + ".")
    if kv.get("from-file"):
        src = kv["from-file"]
        try:
            raw = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            sys.exit("story-gate: can't read %s: %s" % (src, e))
        title, body = kv.get("title", ""), raw
        meta = {"source_kind": kind + "-issue", "source_url": ticket_url(kind, settings, key), "site": settings["workspace"],
                "ticket": key, "ticket_id": "unknown", "source_updated_at": "unknown", "fetched_at": gate.now(),
                "fetched_by": "pasted", "format": FORMAT}
    else:
        tok = local_token(kind)
        if not tok:
            sys.exit("story-gate: no %s key here. Set %s to your own read-only %s API key, or paste the ticket with "
                     "--from-file (for example from your AI tool's %s connector; it's marked 'not verified' until CI "
                     "checks it)." % (LABEL[kind], LOCAL_ENV[kind][1], LABEL[kind], LABEL[kind]))
        try:
            t = FETCH[kind](settings, key, tok)
        except TrackerError as e:
            sys.exit("story-gate: nothing was saved: %s." % e)
        if t["key"] != key:
            sys.exit("story-gate: %s is now %s (moved?). Pull %s instead." % (key, t["key"], t["key"]))
        title, body = t["title"], t["body"]
        meta = {"source_kind": kind + "-issue", "source_url": t["url"], "site": settings["workspace"], "ticket": key,
                "ticket_id": t["id"], "source_updated_at": t["updated_at"], "fetched_at": gate.now(),
                "fetched_by": kind + "-api", "format": FORMAT}
    if not isinstance(body, str) or len(body) > MAX_BODY:
        sys.exit("story-gate: the ticket text is bigger than %d characters; nothing was saved." % MAX_BODY)
    if not body.strip():
        sys.exit("story-gate: %s has no text, so there's nothing to check. Nothing was saved." % key)
    out = sd / ("%s-%s.md" % (kind, key))
    rel = out.relative_to(gate.ROOT).as_posix()
    if gate.tracker_name(rel) != (sid, kind, key):
        sys.exit("story-gate: %s can't be named unambiguously; nothing was saved." % key)
    text = snapshot_text(gate, sid, kind, settings, key, meta, title, body)
    gate.parse_snapshot(text)  # the gate must be able to read what was written
    return write_and_link(text, out, rel, "%s %s" % (LABEL[kind], key), meta["fetched_by"] != "pasted")


def live_status(gate, sid, rel, kind, key, settings, token, fetch=None):
    """CI: compare the copy at rel with the live ticket. Tracker and key come from the file name, the workspace from the
    default branch's config, never from the copy. Returns (state, detail): 'unchanged', 'changed' or 'not checked'."""
    try:
        fields, text = gate.parse_snapshot(gate.rd(gate.ROOT / rel))
    except ValueError as e:
        return "not checked", "the copy can't be read (%s)" % e
    if fields["site"] != settings.get("workspace"):
        return "changed", "the copy says it came from the workspace '%s', but this repository is set up for '%s'" % (
            fields["site"][:60], settings.get("workspace"))
    try:
        t = (fetch or FETCH[kind])(settings, key, token)
    except NotFound as e:
        return "changed", "%s (deleted, moved, or no longer visible to the key)" % e
    except TrackerError as e:
        return "not checked", "%s couldn't be read: %s" % (key, e)
    if t["key"] != key or (fields["ticket_id"] != "unknown" and t["id"] != fields["ticket_id"]):
        return "changed", "%s is now another ticket (%s); pull it again" % (key, t["key"])
    if fields["format"] != FORMAT:
        return "changed", "the copy was made by an older story-gate; pull it again"
    if not isinstance(t["body"], str) or len(t["body"]) > MAX_BODY:
        return "not checked", "the live ticket is too big to compare"
    live = content(sid, key, t["title"], t["body"])
    pasted = fields["fetched_by"] == "pasted"
    if live == text or (pasted and content(sid, key, "", t["body"]) == text):
        return "unchanged", "matches the live ticket" + (" (the pasted copy is now verified)" if pasted else "")
    old_u, new_u = [], []
    old = {k for k, _ in gate.spec_scenarios(text, old_u)}
    new = {k for k, _ in gate.spec_scenarios(live, new_u)}
    diff = []
    if new - old:
        diff.append("%d requirement(s) added or reworded" % len(new - old))
    if old - new:
        diff.append("%d removed or reworded" % len(old - new))
    if len(new_u) > len(old_u):
        diff.append("%d new line(s) that look like requirements but can't be read" % (len(new_u) - len(old_u)))
    return "changed", ("the live ticket differs from the copy (%s). Run `story-gate spec-pull %s %s` and check the "
                       "requirements again" % ("; ".join(diff) or "text outside the requirements changed; read the "
                                               "difference, it may still matter", sid, key))
