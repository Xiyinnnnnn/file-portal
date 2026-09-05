# Agent Integration Guide (not for humans)

Machine-targeted contract documentation. Read if you are an LLM agent tasked
with extending, deploying, auditing or testing this codebase.

## 1. Deployment contract

| knob | env | CLI flag | default | semantics |
|---|---|---|---|---|
| bind host | — | `--host` | `127.0.0.1` | never expose 0.0.0.0 without a terminating proxy |
| port | `FILE_PORTAL_PORT` | `--port` | `8000` | int; parse errors crash on startup (fail fast) |
| root dir | `FILE_PORTAL_ROOT` | `--root` | `$HOME` | expanded + `.resolve()`d once at startup; **cannot be changed at runtime** |
| credentials | `FILE_PORTAL_AUTH` | `--auth` | unset | literal `user:pass`; colon in pass unsupported; empty ⇒ auth OFF (server prints warning) |

Precedence: CLI flag > env var > default. Auth state is resolved in `__main__`
and frozen into module globals `_AUTH_USER`/`_AUTH_PASS`. Handler reads globals,
so tests that mutate `sys.modules[__name__]` globals before serving can fake auth.

## 2. HTTP contract

All responses to HTML routes carry `Content-Type: text/html; charset=utf-8`.
Upload returns `application/json`. Every request without valid Basic credentials
gets `401` + `WWW-Authenticate: Basic realm="file-portal"` + zero-length body.

### GET / — index
200 HTML with a single CTA link to `/files`. No dynamic data. Safe to cache.

### GET /files?path=REL
- REL: URL-encoded relative posix path inside ROOT. Empty = ROOT itself.
- 200: directory listing table. First row is `上级目录` when REL ≠ "".
  Parent href is computed by `rel.rstrip('/').rsplit('/', 1)[0]` — no `.` segments.
  Dot-entries (name starts `.`) are always filtered. Sort: dirs-first then
  name.lower(); deterministic.
- 404 text/plain when REL escapes ROOT or resolves to a non-directory.
- Also 404 text/plain on `PermissionError` (os error surface, not auth failure —
  do not confuse with 401).

### GET /file?path=REL
- REL must resolve inside ROOT and to a regular file, else 404 text/plain.
- 200 stream: `Content-Type` from mimetypes guess (fallback octet-stream),
  `Content-Length` exact, `Content-Disposition: attachment` with ASCII fallback
  name + RFC 5987 `filename*=UTF-8''` for non-ASCII. Chunked 64 KiB writes.
- No range support, no conditional headers, no cache headers. Agent adding
  resume support must touch `_serve_file` only.

### POST /upload?path=REL
- Body: `multipart/form-data`, single field `file`. Other fields ignored.
- 200 JSON `{"ok": true, "msg": "已上传 {name}（{size}）到 {dir}"}`.
- 400/404 JSON `{"ok": false, "msg": "..."}` on: target invalid, missing
  boundary, empty body, malformed framing, no filename part.
- Filename is `os.path.basename`d → client cannot inject path separators.
- Collision policy: `name.ext` exists ⇒ try `name_1.ext`, `name_2.ext`, …
  (before first `.`, integer suffix, monotonic within one call, stat-racy across
  concurrent uploads — acceptable single-user scope).
- Parser is hand-rolled (read full body into RAM via Content-Length). Max size
  is unbounded by code. Agent hardening: add a size cap before `rfile.read`.

## 3. Security model — read before touching anything

Trust boundary: the HTTP client is **untrusted**. Everything between `do_GET`
entry and `_resolve` output is attacker-controlled.

1. Auth is the only gate. `_auth_ok` compares base64-decoded header against
   frozen globals using plain `==` (not hmac.compare_digest → timing side
   channel exists; acceptable for personal use, flag if you harden).
2. `_resolve(rel)` → `(ROOT / rel).resolve()`. Symlink escape is defeated by
   `.resolve()` resolving the final target. Prefix check is
   `p == ROOT or ROOT in p.parents` — do **not** "simplify" to
   `str(p).startswith(str(ROOT))` (would allow `/root2` prefix confusion).
3. Race: TOCTOU between `_resolve` and `open()` — a local attacker able to swap
   symlinks inside ROOT could redirect `_serve_file`. Threat requires local
   write access to ROOT; out of scope, document as known limitation.
4. `..` and `.` in REL survive parse_qs unencoded? URL-decoded by parse_qs then
   handed to Path — containment enforced purely by `_resolve`, never by string
   filtering. Keep it that way.
5. XSS surface: all names/paths are `html.escape`d (attr context with
   `quote=True` for query values). `_serve_404` msg is a server-side constant —
   never echo client input through it.

Auth-disabled mode (`_AUTH_USER is None`) must never reach a public tunnel.
Server prints `auth=OFF` at startup — agents should grep startup line in tests.

## 4. Extension map (where new code goes)

| change | location |
|---|---|
| new page route | `do_GET` if-chain + new HTML template constant near `LIST_PAGE` |
| new JSON API | `do_POST` if-chain + `_json` helper |
| auth policy change | `_auth_ok` + `__main__` freeze block |
| path policy change | `_resolve` only |
| file ops behavior | `_serve_listing` / `_serve_file` / `_upload` |
| UI copy | template constants `INDEX_PAGE` / `LIST_PAGE` |

Module is 284 LOC, zero imports outside stdlib. Keep it dependency-free; a new
stdlib import is acceptable, a third-party one is not (project identity).

## 5. Regression vectors (run after any change)

```bash
SRV=http://127.0.0.1:PORT; A='user:pass'; T=$(mktemp -d); echo x > $T/f.txt
FILE_PORTAL_PORT=PORT FILE_PORTAL_AUTH=$A python3 file_portal.py --root $T &
# 1 auth on
test "$(curl -s -o /dev/null -w '%{http_code}' $SRV/)" = 401
# 2 index + listing + download
test "$(curl -s -u $A -o /dev/null -w '%{http_code}' $SRV/)" = 200
curl -s -u $A "$SRV/files" | grep -q f.txt
curl -s -u $A "$SRV/file?path=f.txt" | grep -q x
# 3 traversal must 404 (raw and double-encoded)
test "$(curl -s -u $A -o /dev/null -w '%{http_code}' "$SRV/file?path=../etc/passwd")" = 404
test "$(curl -s -u $A -o /dev/null -w '%{http_code}' "$SRV/file?path=%2e%2e%2fetc%2fpasswd")" = 404
# 4 upload round-trip
curl -s -u $A -F "file=@$T/f.txt" "$SRV/upload" | grep -q '"ok": true'
kill %1
```

## 6. Known limitations (deliberate)

- no auth brute-force throttling (personal scope)
- upload size unbounded (RAM buffering)
- no HTTPS on its own; TLS terminates upstream (proxy/tunnel)
- listing skips dotfiles but serves them via /file if path known
- plain `==` credential compare (timing)
- single-user, single-process, no session cookies
