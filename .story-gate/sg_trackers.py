"""Tickets kept in a tracker: Linear, Jira Cloud, and Jira Server / Data Center (8.14 or later).

One contract per tracker: fetch(settings, ticket, token) -> {"id", "key", "title", "body", "url", "updated_at"} or raises
TrackerError with plain words. Read-only, a fixed HTTPS endpoint built here (never taken from a copy or a ticket), no
redirects followed, bounded time and size, the token sent only to that endpoint and never printed. The copy itself is
written and checked like a GitHub issue copy: the gate reads only the copy; only CI's own comparison can call it verified.
"""
import base64, json, os, re, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

FORMAT = "1"                 # how a ticket becomes the copy's text; bump it if that ever changes, so old copies show "changed"
MAX_BYTES = 1_000_000        # an answer bigger than this isn't one ticket
MAX_BODY = 256_000
TIMEOUT = 20
TICKET = re.compile(r"[A-Z][A-Z0-9]{0,9}-[0-9]{1,9}")
WORKSPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
JIRA_SITE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.atlassian\.net")
JIRA_FIELD = re.compile(r"customfield_[0-9]{1,12}")
# a Jira Cloud site's id: a UUID, sometimes with a suffix on older sites (e.g. '...-3c15c2bd8caa-ecosystem')
# Jira Server / Data Center: host[:port][/context path], no scheme (always https), no query, no user info
JIRA_SERVER = re.compile(r"(?!localhost\b)(?![0-9.]+(?:[:/]|$))"           # a name, never localhost or a bare IP address
                         r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+"
                         r"(?::(?:[1-9][0-9]{1,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?"
                         r"(?:/(?!\.{1,2}(?:/|$))[A-Za-z0-9._~-]{1,64}){0,5}")
CLOUD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:-[a-z0-9]{1,40})?")
LABEL = {"linear": "Linear", "jira": "Jira"}
# the CI secrets (read-only); every one must be set or the tickets show "not checked"
CI_SECRETS = {"linear": ("STORY_GATE_LINEAR_KEY",), "jira": ("STORY_GATE_JIRA_EMAIL", "STORY_GATE_JIRA_TOKEN")}
TOKEN_ENV = {k: v[-1] for k, v in CI_SECRETS.items()}
# on your own computer: your own read-only credentials (the CI names work too)
LOCAL_ENV = {"linear": (("STORY_GATE_LINEAR_KEY", "LINEAR_API_KEY"),),
             "jira": (("STORY_GATE_JIRA_EMAIL", "JIRA_EMAIL"), ("STORY_GATE_JIRA_TOKEN", "JIRA_API_TOKEN"))}
NOTE = "<!-- story-gate snapshot: don't edit by hand. Run `story-gate spec-pull %s %s` again to update it. -->"


class TrackerError(Exception):
    """Plain-English reason a ticket couldn't be read. Never carries the token."""


class NotFound(TrackerError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could carry the token elsewhere: never follow one
        return None


def post_json(url, headers, payload, tries=2, sleep=time.sleep):
    """(HTTP status, parsed JSON or None) for a POST. See request_json."""
    return request_json(url, headers, payload, tries, sleep)


def get_json(url, headers, tries=2, sleep=time.sleep):
    return request_json(url, headers, None, tries, sleep)


def request_json(url, headers, payload, tries=2, sleep=time.sleep):
    """(HTTP status, parsed JSON or None). Honours HTTPS_PROXY and the system's CA certificates (SSL_CERT_FILE), verifies
    TLS, follows no redirect, reads at most MAX_BYTES, waits at most TIMEOUT per try and retries once on 429/5xx."""
    if not url.startswith("https://") and not os.environ.get("STORY_GATE_TEST_HTTP"):
        raise TrackerError("refusing a non-HTTPS address")
    opener = urllib.request.build_opener(_NoRedirect)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdrs = dict(headers, **{"Accept": "application/json", "User-Agent": "story-gate"})
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    host = url.split("/")[2]
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST", headers=hdrs)
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


# ---------------------------------------------------------------- Jira Cloud
JIRA_GATEWAY = "https://api.atlassian.com/ex/jira/%s/rest/api/3"   # scoped API tokens are used through Atlassian's gateway
ADF_MAX_DEPTH = 40


def jira_auth(token):
    """token is 'email:api-token' (both from the environment); Atlassian takes it as HTTP basic auth."""
    return {"Authorization": "Basic " + base64.b64encode(token.encode("utf-8")).decode("ascii")}


def _ac_heading(text):
    return re.fullmatch(r"\s*acceptance\s+criteria\s*:?\s*", text or "", re.I) is not None


def adf_to_text(node, ac=False, _depth=0):
    """Atlassian Document Format (Jira's rich text) to markdown story-gate can read. Unknown content (a macro, an
    extension, a synced block, anything not listed) fails closed: no partial text is ever saved or verified.
    ac=True (the acceptance-criteria field): every list item becomes a '- [ ]' criterion. In a description, list items
    right under an 'Acceptance criteria' heading become criteria too; elsewhere lists stay plain."""
    out = []

    def inline(nodes, depth):
        parts = []
        for n in nodes or []:
            if not isinstance(n, dict):
                raise TrackerError("Jira's text has an unreadable part")
            t = n.get("type")
            a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
            if t == "text":
                if not isinstance(n.get("text"), str):
                    raise TrackerError("Jira's text has an unreadable part")
                parts.append(n["text"])
            elif t == "hardBreak":
                parts.append(" ")
            elif t == "mention":
                parts.append("@" + str(a.get("text") or "someone").lstrip("@"))
            elif t == "emoji":
                parts.append(str(a.get("text") or a.get("shortName") or ""))
            elif t == "inlineCard":
                parts.append(str(a.get("url") or ""))
            elif t == "date":
                parts.append(str(a.get("timestamp") or ""))
            elif t == "status":
                parts.append("[%s]" % str(a.get("text") or ""))
            elif t == "mediaInline":
                parts.append("[attachment]")
            else:
                raise TrackerError("Jira's text has content story-gate can't read yet ('%s'), so nothing was taken from it"
                                   % str(t)[:40])
        return "".join(parts).strip()

    def kids_of(n):
        k = n.get("content") if isinstance(n, dict) else None
        if k is None:
            return []
        if not isinstance(k, list):
            raise TrackerError("Jira's text has an unreadable part")
        return k

    def block(n, depth, indent, ctx):
        if depth + _depth > ADF_MAX_DEPTH:
            raise TrackerError("Jira's text is nested too deeply to read")
        if not isinstance(n, dict):
            raise TrackerError("Jira's text has an unreadable part")
        t, kids = n.get("type"), n.get("content") or []
        if not isinstance(kids, list):
            raise TrackerError("Jira's text has an unreadable part")
        a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
        pad = " " * indent
        if t == "doc":
            for k in kids:
                block(k, depth + 1, indent, ctx)
        elif t == "paragraph":
            text = inline(kids, depth)
            if text:
                out.append(pad + text)
        elif t == "heading":
            text = inline(kids, depth)
            level = a.get("level") if isinstance(a.get("level"), int) and 1 <= a.get("level") <= 6 else 2
            out.append("\n%s %s\n" % ("#" * level, text))
            if _ac_heading(text):
                ctx["ac"], ctx["ac_level"] = True, level
            elif not (ctx["ac"] and level > ctx.get("ac_level", 0)):  # a deeper heading stays inside the section
                ctx["ac"] = ctx["ac_field"]
        elif t in ("bulletList", "orderedList"):
            for i, item in enumerate(kids, 1):
                if not isinstance(item, dict) or item.get("type") != "listItem":
                    raise TrackerError("Jira's list has an unreadable item")
                mark = "- [ ] " if ctx["ac"] else ("- " if t == "bulletList" else "%d. " % i)
                list_item(item, depth + 1, indent, mark, ctx)
        elif t in ("taskList", "decisionList"):
            for item in kids:
                it = item.get("type") if isinstance(item, dict) else None
                if it in ("taskItem", "blockTaskItem"):
                    ia = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
                    mark = "- [x] " if ia.get("state") == "DONE" else "- [ ] "
                elif it == "decisionItem":
                    mark = "- [ ] " if ctx["ac"] else "- "
                elif it == "taskList":
                    block(item, depth + 1, indent + 2, ctx)
                    continue
                else:
                    raise TrackerError("Jira's task list has an unreadable item")
                if it == "blockTaskItem":
                    list_item(item, depth + 1, indent, mark, ctx)
                else:
                    out.append(pad + mark + inline(kids_of(item), depth))
        elif t == "codeBlock":
            code = "".join(str(k.get("text", "")) for k in kids if isinstance(k, dict))
            fence = "`" * max(3, 1 + max((len(r) for r in re.findall(r"`+", code)), default=0))  # never closed by its own text
            out.append(pad + fence + "\n" + code + "\n" + pad + fence)
        elif t in ("blockquote", "panel", "expand", "nestedExpand"):
            if a.get("title"):
                out.append(pad + str(a["title"]))
            for k in kids:
                block(k, depth + 1, indent, ctx)
        elif t == "rule":
            out.append(pad + "---")
        elif t in ("mediaSingle", "mediaGroup", "media"):
            out.append(pad + "[attachment]")
        elif t == "table":
            if ctx["ac"]:  # criteria laid out in a table can't be read one by one: stop rather than lose them
                raise TrackerError("Jira's acceptance criteria are in a table, which story-gate can't read as criteria yet; "
                                   "use a list")
            for row in kids:
                if not isinstance(row, dict) or row.get("type") != "tableRow":
                    raise TrackerError("Jira's table has an unreadable row")
                cells = []
                for cell in kids_of(row):
                    if not isinstance(cell, dict) or cell.get("type") not in ("tableCell", "tableHeader"):
                        raise TrackerError("Jira's table has an unreadable cell")
                    sub_out = adf_to_text({"type": "doc", "content": kids_of(cell)}, _depth=depth + _depth + 1)
                    if re.search(r"(?m)^\s*- \[[ xX]\]", sub_out):
                        raise TrackerError("Jira's text has a checklist inside a table, which story-gate can't read as "
                                           "criteria yet; use a list")
                    cells.append(re.sub(r"\s+", " ", sub_out).strip().replace("|", "/"))
                out.append(pad + "| " + " | ".join(cells) + " |")
        else:
            raise TrackerError("Jira's text has content story-gate can't read yet ('%s'), so nothing was taken from it"
                               % str(t)[:40])

    def list_item(item, depth, indent, mark, ctx):
        first = True
        for k in kids_of(item):
            if isinstance(k, dict) and k.get("type") == "paragraph" and first:
                out.append(" " * indent + mark + inline(k.get("content"), depth))
                first = False
            else:
                if first:
                    out.append(" " * indent + mark.rstrip())
                    first = False
                inner = dict(ctx, ac=ctx["ac"] if k.get("type") in ("bulletList", "orderedList") else False) if isinstance(k, dict) else ctx
                block(k, depth + 1, indent + 2, inner)

    if node is None:
        return ""
    if not isinstance(node, dict) or node.get("type") != "doc":
        raise TrackerError("Jira's text isn't in the format story-gate reads")
    try:
        block(node, 0, 0, {"ac": ac, "ac_field": ac})
    except RecursionError:
        raise TrackerError("Jira's text is nested too deeply to read")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def jira_ac_text(value):
    """The acceptance-criteria field as '- [ ]' lines: rich text (ADF) or plain text (one criterion per line)."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return adf_to_text(value, ac=True)
    if isinstance(value, str):
        # only a real list marker followed by a space is dropped: '1.5 seconds' and '*Must* log out' stay whole
        lines = [re.sub(r"^\s*(?:[*#•+]+\s+|-\s+|\d{1,3}[.)]\s+|\[[ xX]?\](?:\s+|$))", "", ln).strip() for ln in value.splitlines()]
        return "\n".join("- [ ] " + ln for ln in lines if ln)
    raise TrackerError("Jira's acceptance-criteria field isn't text (it's a %s); pick a text field" % type(value).__name__)


def jira_fetch(settings, ticket, token, get=None):
    get = get or get_json
    base = JIRA_GATEWAY % settings["cloud_id"]
    fields = ["summary", "description", "updated"] + ([settings["ac_field"]] if settings.get("ac_field") else [])
    st, data = get("%s/issue/%s?fields=%s" % (base, ticket, ",".join(fields)), jira_auth(token))
    if st in (401, 403):
        raise TrackerError("Jira refused the token (HTTP %s); it needs the read:jira-work scope" % st)
    if st == 404:
        raise NotFound("%s wasn't found in Jira (or this token can't see it)" % ticket)
    if st != 200 or not isinstance(data, dict):
        raise TrackerError("Jira answered HTTP %s" % st)
    f = data.get("fields")
    if not isinstance(f, dict) or not isinstance(data.get("key"), str) or not isinstance(data.get("id"), str):
        raise TrackerError("Jira's answer isn't a ticket")
    key, tid = data["key"], data["id"]
    if not TICKET.fullmatch(key) or not re.fullmatch(r"[0-9]{1,18}", tid):
        raise TrackerError("Jira's answer has an unexpected ticket id")
    if not str(data.get("self") or "").startswith(base + "/issue/"):
        raise TrackerError("Jira's answer points somewhere else")
    if not isinstance(f.get("summary"), str) or not isinstance(f.get("updated"), str):
        raise TrackerError("Jira's answer has no summary")
    body = adf_to_text(f.get("description"))
    if settings.get("ac_field"):
        if settings["ac_field"] not in f:
            raise TrackerError("Jira's answer has no field %s (trackers.jira.ac_field); is it on this ticket's screen?" % settings["ac_field"])
        ac = jira_ac_text(f.get(settings["ac_field"]))
        if ac:
            body = (body + "\n\n" if body else "") + "## Acceptance criteria\n\n" + ac
    return {"id": tid, "key": key, "title": f["summary"], "body": body, "updated_at": f["updated"][:40],
            "url": "https://%s/browse/%s" % (settings["site"], key)}


WIKI_CODE = re.compile(r"\{(code|noformat)(?::[^}]*)?\}(.*?)\{\1\}", re.S)
WIKI_OPEN = re.compile(r"\{(?:code|noformat)(?::[^}]*)?\}")
WIKI_TAG = re.compile(r"\{(?:quote|panel(?::[^}]*)?|color(?::[^}]*)?)\}")


def wiki_to_text(text, ac=False):
    """Jira Server / Data Center's wiki markup to markdown story-gate can read. Lists under an 'Acceptance criteria'
    heading (or every list when ac=True) become '- [ ]' criteria; {code} and {noformat} blocks, wherever they open, are
    kept as code on lines of their own; criteria in a table, or a code block that never closes, stop the copy (nothing is
    half-read). Other text is kept as written."""
    if text is None:
        return ""
    if not isinstance(text, str):
        raise TrackerError("Jira's description isn't text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out, state = [], {"ac": ac, "level": 0}

    def prose(chunk):
        if WIKI_OPEN.search(chunk):
            raise TrackerError("Jira's text has a {code} or {noformat} block that never closes")
        for ln in chunk.split("\n"):
            ln = WIKI_TAG.sub("", ln)
            ln = re.sub(r"\{\{(.+?)\}\}", r"`\1`", ln)
            s_ = ln.strip()
            h = re.match(r"^h([1-6])\.\s*(.*)$", s_)
            lm = re.match(r"^([*#]+|-)\s+(.*)$", s_)
            if h:
                lvl = int(h.group(1))
                out.append("\n%s %s\n" % ("#" * lvl, h.group(2).strip()))
                if _ac_heading(h.group(2)):
                    state["ac"], state["level"] = True, lvl
                elif not (state["ac"] and lvl > state["level"]):  # a deeper heading stays inside the section, as the gate reads it
                    state["ac"] = ac
            elif lm:
                depth = len(lm.group(1)) if lm.group(1) != "-" else 1
                mark = "- [ ] " if state["ac"] else ("1. " if lm.group(1).endswith("#") else "- ")
                out.append("  " * (depth - 1) + mark + lm.group(2).strip())
            elif s_.startswith("|"):
                if state["ac"]:
                    raise TrackerError("Jira's acceptance criteria are in a table, which story-gate can't read as criteria "
                                       "yet; use a list")
                out.append(s_)
            elif s_ == "----":
                out.append("---")
            elif s_.startswith("bq."):
                out.append("> " + s_[3:].strip())
            else:
                out.append(ln.rstrip())

    pos = 0
    for m in WIKI_CODE.finditer(text):  # each block is matched with its own closing tag, so a {code} inside {noformat} is text
        prose(text[pos:m.start()])
        code = m.group(2).strip("\n")
        fence = "`" * max(3, 1 + max((len(r) for r in re.findall(r"`+", code)), default=0))
        out.append(fence + "\n" + code + "\n" + fence)
        pos = m.end()
    prose(text[pos:])
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def jira_server_fetch(settings, ticket, token, get=None):
    """Jira Server / Data Center (REST v2) with a personal access token (read-only account)."""
    get = get or get_json
    base = "https://%s/rest/api/2" % settings["site"]
    fields = ["summary", "description", "updated"] + ([settings["ac_field"]] if settings.get("ac_field") else [])
    st, data = get("%s/issue/%s?fields=%s" % (base, ticket, ",".join(fields)), {"Authorization": "Bearer " + token})
    if st in (401, 403):
        raise TrackerError("Jira refused the token (HTTP %s)" % st)
    if st == 404:
        raise NotFound("%s wasn't found in Jira (or this token can't see it)" % ticket)
    if st != 200 or not isinstance(data, dict):
        raise TrackerError("Jira answered HTTP %s" % st)
    f = data.get("fields")
    if not isinstance(f, dict) or not isinstance(data.get("key"), str) or not isinstance(data.get("id"), str):
        raise TrackerError("Jira's answer isn't a ticket")
    key, tid = data["key"], data["id"]
    if not TICKET.fullmatch(key) or not re.fullmatch(r"[0-9]{1,18}", tid):
        raise TrackerError("Jira's answer has an unexpected ticket id")
    if not str(data.get("self") or "").startswith(base + "/issue/"):
        raise TrackerError("Jira's answer points somewhere else")
    if not isinstance(f.get("summary"), str) or not isinstance(f.get("updated"), str):
        raise TrackerError("Jira's answer has no summary")
    body = wiki_to_text(f.get("description"))
    if settings.get("ac_field"):
        if settings["ac_field"] not in f:
            raise TrackerError("Jira's answer has no field %s (trackers.jira.ac_field); is it on this ticket's screen?" % settings["ac_field"])
        v = f.get(settings["ac_field"])
        ac = wiki_to_text(v, ac=True) if isinstance(v, str) and re.search(r"(?m)^\s*(?:[*#]+|-)\s", v) else jira_ac_text(v)
        if ac:
            body = (body + "\n\n" if body else "") + "## Acceptance criteria\n\n" + ac
    return {"id": tid, "key": key, "title": f["summary"], "body": body, "updated_at": f["updated"][:40],
            "url": "https://%s/browse/%s" % (settings["site"], key)}


def jira_any_fetch(settings, ticket, token, get=None):
    return (jira_server_fetch if settings.get("server") else jira_fetch)(settings, ticket, token, get=get)


def jira_setup(args, env=os.environ):
    """story-gate jira-setup <site> ["field name"]: print the trackers.jira settings for a Jira Cloud site: its cloud id
    (public), and, with your read-only token, the id of the acceptance-criteria field found by its name."""
    site = (args[0] if args else "").strip().rstrip("/")
    site = site[len("https://"):] if site.startswith("https://") else site
    if not site or not (JIRA_SITE.fullmatch(site) or JIRA_SERVER.fullmatch(site)):
        sys.exit("usage: jira-setup <your-site.atlassian.net | your Jira Server address, e.g. jira.acme.com> "
                 "[\"Acceptance criteria field name\"]")
    if len(args) > 1 and not args[1].strip():
        sys.exit("story-gate: give the field's name, e.g. \"Acceptance criteria\".")
    if not JIRA_SITE.fullmatch(site):
        return jira_server_setup(site, args[1] if len(args) > 1 else None, env)
    st, info = get_json("https://%s/_edge/tenant_info" % site, {})
    cloud = (info or {}).get("cloudId") if isinstance(info, dict) else None
    if st != 200 or not isinstance(cloud, str) or not CLOUD_ID.fullmatch(cloud):
        sys.exit("story-gate: couldn't read %s's cloud id (HTTP %s). Is the site name right?" % (site, st))
    settings = {"site": site, "cloud_id": cloud}
    if len(args) > 1:
        tok = local_token("jira", env)
        if not tok:
            sys.exit("story-gate: to find the field, set JIRA_EMAIL and JIRA_API_TOKEN (your own read-only token) first.")
        st, fields = get_json((JIRA_GATEWAY % cloud) + "/field", jira_auth(tok))
        if st != 200 or not isinstance(fields, list):
            sys.exit("story-gate: Jira answered HTTP %s when listing fields." % st)
        settings["ac_field"] = find_field(fields, args[1])
    print('Add this to .story-gate/config.json in a pull request:\n  "trackers": {"jira": %s}' % json.dumps(settings))
    return 0


def match_field(fields, name):
    """(id of the one custom field called `name` (any case) or None, [close matches])."""
    if not isinstance(fields, list):
        return None, []
    want = name.strip().lower()
    hits = [x for x in fields if isinstance(x, dict) and str(x.get("name", "")).strip().lower() == want
            and JIRA_FIELD.fullmatch(str(x.get("id", "")))]
    if len(hits) == 1:
        return hits[0]["id"], []
    first = (want.split() or [""])[0]
    return None, sorted({str(x.get("name"))[:60] for x in fields if isinstance(x, dict) and first in str(x.get("name", "")).lower()})[:10]


def find_field(fields, name):
    """The id of the one custom field called `name` (any case); exits with close matches otherwise."""
    if not isinstance(fields, list):
        sys.exit("story-gate: Jira didn't list its fields.")
    fid, names = match_field(fields, name)
    if not fid:
        n = sum(1 for x in fields if isinstance(x, dict) and str(x.get("name", "")).strip().lower() == name.strip().lower())
        sys.exit("story-gate: %s fields named '%s' found. Close matches: %s" % (n or "no", name, ", ".join(names) or "none"))
    return fid


def jira_server_setup(site, field_name, env):
    settings = {"site": site, "server": True}
    if field_name:
        tok = _first(("STORY_GATE_JIRA_TOKEN", "JIRA_API_TOKEN"), env)
        if not tok:
            sys.exit("story-gate: to find the field, set JIRA_API_TOKEN to your own personal access token first.")
        st, fields = get_json("https://%s/rest/api/2/field" % site, {"Authorization": "Bearer " + tok})
        if st != 200:
            sys.exit("story-gate: Jira answered HTTP %s when listing fields." % st)
        settings["ac_field"] = find_field(fields, field_name)
    print('Add this to .story-gate/config.json in a pull request:\n  "trackers": {"jira": %s}' % json.dumps(settings))
    return 0


FETCH = {"linear": linear_fetch, "jira": jira_any_fetch}


def site_of(kind, settings):
    """The one value a copy is bound to: the Linear workspace, or the Jira site."""
    return settings.get("workspace") if kind == "linear" else settings.get("site")


def ticket_url(kind, settings, key):
    if kind == "linear":
        return "https://linear.app/%s/issue/%s" % (settings.get("workspace"), key)
    if kind == "jira":
        return "https://%s/browse/%s" % (settings.get("site"), key)
    raise TrackerError("unknown tracker %s" % kind)


def url_of(kind, site, key):
    return ticket_url(kind, {"workspace": site, "site": site}, key)


def site_ok(kind, site):
    if kind == "linear":
        return bool(WORKSPACE.fullmatch(site or ""))
    return bool(JIRA_SITE.fullmatch(site or "") or JIRA_SERVER.fullmatch(site or ""))


def settings_problem(kind, v):
    """None when trackers.<kind> is well formed, else what's wrong (plain words)."""
    if not isinstance(v, dict):
        return "must be an object"
    if not isinstance(v.get("allow_in_public_repo", False), bool):
        return '"allow_in_public_repo" must be true or false'
    if kind == "linear":
        extra = set(v) - {"workspace", "allow_in_public_repo"}
        if extra or not isinstance(v.get("workspace"), str) or not WORKSPACE.fullmatch(v["workspace"]):
            return 'must look like {"workspace": "your-workspace"}'
        return None
    if kind == "jira" and "server" in v:
        extra = set(v) - {"site", "server", "ac_field", "allow_in_public_repo"}
        if extra or v.get("server") is not True or not isinstance(v.get("site"), str) or not JIRA_SERVER.fullmatch(v["site"]) \
                or JIRA_SITE.fullmatch(v["site"]) \
                or ("ac_field" in v and not (isinstance(v["ac_field"], str) and JIRA_FIELD.fullmatch(v["ac_field"]))):
            return ('for Jira Server / Data Center must look like {"site": "jira.acme.com", "server": true, '
                    '"ac_field": "customfield_10050" (optional)}')
        return None
    if kind == "jira":
        extra = set(v) - {"site", "cloud_id", "ac_field", "allow_in_public_repo"}
        if extra or not isinstance(v.get("site"), str) or not JIRA_SITE.fullmatch(v["site"]) \
                or not isinstance(v.get("cloud_id"), str) or not CLOUD_ID.fullmatch(v["cloud_id"]) \
                or ("ac_field" in v and not (isinstance(v["ac_field"], str) and JIRA_FIELD.fullmatch(v["ac_field"]))):
            return ('must look like {"site": "acme.atlassian.net", "cloud_id": "<from story-gate jira-setup>", '
                    '"ac_field": "customfield_10050" (optional)}')
        return None
    return "isn't a tracker story-gate knows (linear, jira)"


def parse_ticket_ref(ref):
    """(tracker or None, 'ENG-12', site or None) for 'ENG-12', a Linear issue URL or a Jira Cloud /browse/ URL; None if it
    isn't a ticket."""
    ref = (ref or "").strip()
    if TICKET.fullmatch(ref):
        return None, ref, None
    m = re.fullmatch(r"https://linear\.app/([a-z0-9-]{1,48})/issue/([A-Z][A-Z0-9]{0,9}-[0-9]{1,9})(?:/[^\s]*)?", ref)
    if m:
        return "linear", m.group(2), m.group(1)
    m = re.fullmatch(r"https://([^\s/?#@]+(?:/[A-Za-z0-9._~-]{1,64}){0,5})/browse/([A-Z][A-Z0-9]{0,9}-[0-9]{1,9})/?(?:[?#][^\s]*)?", ref)
    if m and (JIRA_SITE.fullmatch(m.group(1)) or JIRA_SERVER.fullmatch(m.group(1))):
        return "jira", m.group(2), m.group(1)  # the site must still be the one in config (spec-pull checks)
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


def _first(names, env, saved=None):
    """The first of these environment variables that is set; else the copy `story-gate tracker-setup` saved on this
    computer when the person ticked that box (owner-only, in the story-gate user folder)."""
    for name in names:
        v = (env.get(name) or "").strip()
        if v:
            return v
    return (saved or {}).get(names[0], "")


def saved_keys():
    """{STORY_GATE_* name: value} from tracker-setup's opt-in local copy; {} when there is none or it can't be read."""
    try:
        import sg_trackersetup as TS
        return TS.read_local()
    except Exception:
        return {}


def secrets_for(kind, settings=None):
    """The CI secrets a tracker needs: Jira Server / Data Center takes a personal access token alone."""
    if kind == "jira" and (settings or {}).get("server"):
        return ("STORY_GATE_JIRA_TOKEN",)
    return CI_SECRETS[kind]


def local_token(kind, env=os.environ, settings=None):
    """The credential for this computer: Linear's key, Jira Cloud's 'email:token', or Jira Server's personal access
    token; '' when anything is missing."""
    groups = LOCAL_ENV[kind][-1:] if kind == "jira" and (settings or {}).get("server") else LOCAL_ENV[kind]
    saved = saved_keys() if env is os.environ else {}
    vals = [_first(names, env, saved) for names in groups]
    return ":".join(vals) if all(vals) else ""


def ci_token(kind, env=os.environ, settings=None):
    """(credential or '', names of missing CI secrets)."""
    names = secrets_for(kind, settings)
    vals = [(env.get(n) or "").strip() for n in names]
    missing = [n for n, v in zip(names, vals) if not v]
    return ("" if missing else ":".join(vals)), missing


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


def untrusted_server(gate, settings):
    """Your Jira Server token goes only to the address on the default branch's config (or one you name yourself in
    STORY_GATE_JIRA_SITE), so a branch can't point it at another server. None when the address is trusted."""
    site = settings.get("site")
    named = (os.environ.get("STORY_GATE_JIRA_SITE") or "").strip().rstrip("/")
    named = named[len("https://"):] if named.startswith("https://") else named  # the same forms jira-setup takes
    if named == site:
        return None
    base = gate.cfg().get("base_branch", "main")
    for ref in ("origin/" + base, base):
        raw = gate.git("show", "%s:.story-gate/config.json" % ref)
        if raw:
            try:
                theirs = ((json.loads(raw).get("trackers") or {}).get("jira") or {})
            except (ValueError, AttributeError):
                theirs = {}
            if isinstance(theirs, dict) and theirs.get("server") and theirs.get("site") == site:
                return None
            break
    return ("this branch's config sends your Jira token to %s, which isn't the address on %s's config. If that's your "
            "Jira, set STORY_GATE_JIRA_SITE=%s and run it again." % (site, base, site))


def cli(gate, sid, ref_arg, kv, sd, write_and_link):
    """spec-pull for a tracker ticket. `write_and_link(text, out, key)` is spec-pull's own safe write and story.md link."""
    parsed = parse_ticket_ref(ref_arg)
    trackers = gate.cfg().get("trackers") or {}
    kind, key, ws = parsed
    if kv.get("tracker"):
        if kv["tracker"] not in FETCH or (kind and kind != kv["tracker"]):
            sys.exit("story-gate: --tracker must be one of %s (and match the link)." % ", ".join(sorted(FETCH)))
        kind = kv["tracker"]
    if kind is None:
        set_up = [k for k in sorted(FETCH) if isinstance(trackers.get(k), dict)]
        if not set_up:
            sys.exit("story-gate: %s looks like a Linear or Jira ticket, but no tracker is set up. Add it to "
                     ".story-gate/config.json, e.g. \"trackers\": {\"linear\": {\"workspace\": \"your-workspace\"}} "
                     "(see the guide, 'Specs in Linear' or 'Specs in Jira')." % key)
        if len(set_up) > 1:
            sys.exit("story-gate: both %s are set up; say which with --tracker %s, or paste the ticket's link."
                     % (" and ".join(LABEL[k] for k in set_up), "|".join(set_up)))
        kind = set_up[0]
    settings = trackers.get(kind)
    if not isinstance(settings, dict):
        sys.exit("story-gate: %s isn't set up in .story-gate/config.json (trackers.%s)." % (LABEL[kind], kind))
    site = site_of(kind, settings)
    if ws and ws != site:
        sys.exit("story-gate: that link is in %s '%s', but this repository is set up for '%s'." % (LABEL[kind], ws, site))
    why = public_repo_refused(gate, settings)
    if why:
        sys.exit("story-gate: nothing was copied: " + why + ".")
    if kind == "jira" and settings.get("server") and not kv.get("from-file"):
        why = untrusted_server(gate, settings)
        if why:
            sys.exit("story-gate: nothing was fetched: " + why)
    if kv.get("from-file"):
        src = kv["from-file"]
        try:
            raw = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            sys.exit("story-gate: can't read %s: %s" % (src, e))
        title, body = kv.get("title", ""), raw
        meta = {"source_kind": kind + "-issue", "source_url": ticket_url(kind, settings, key), "site": site,
                "ticket": key, "ticket_id": "unknown", "source_updated_at": "unknown", "fetched_at": gate.now(),
                "fetched_by": "pasted", "format": FORMAT}
    else:
        tok = local_token(kind, settings=settings)
        if not tok:
            groups = LOCAL_ENV[kind][-1:] if kind == "jira" and settings.get("server") else LOCAL_ENV[kind]
            sys.exit("story-gate: no %s key here. Run `story-gate tracker-setup` yourself (not your AI) and tick 'keep a "
                     "copy on this computer', or set %s to your own read-only %s credentials, or paste the ticket with "
                     "--from-file (for example from your AI tool's %s connector; it's marked 'not verified' until CI "
                     "checks it)." % (LABEL[kind], " and ".join(n[-1] for n in groups), LABEL[kind], LABEL[kind]))
        try:
            t = FETCH[kind](settings, key, tok)
        except TrackerError as e:
            sys.exit("story-gate: nothing was saved: %s." % e)
        if t["key"] != key:
            sys.exit("story-gate: %s is now %s (moved?). Pull %s instead." % (key, t["key"], t["key"]))
        title, body = t["title"], t["body"]
        meta = {"source_kind": kind + "-issue", "source_url": t["url"], "site": site, "ticket": key,
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
    if fields["site"] != site_of(kind, settings):
        return "changed", "the copy says it came from '%s', but this repository is set up for '%s'" % (
            fields["site"][:80], site_of(kind, settings))
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
