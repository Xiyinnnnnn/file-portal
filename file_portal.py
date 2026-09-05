#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""File Portal — a super-lightweight password-protected web file manager.

Routes:
  GET  /                 index page (link into the file manager)
  GET  /files?path=REL   browse directory (REL is relative to $FILE_PORTAL_ROOT)
  GET  /file?path=REL    download a file (Content-Disposition attachment)
  POST /upload?path=REL  upload a file into REL

Security model:
  * HTTP Basic Auth. Credentials come from --auth "user:pass" / $FILE_PORTAL_AUTH.
  * Every path is resolved against FILE_PORTAL_ROOT and must stay inside it,
    so ../ traversal can never escape the exposed root.
  * Listens on 127.0.0.1 only. Expose it with your own reverse proxy / tunnel
    (e.g. cloudflared). Remember: that tunnel is your real attack surface.
"""
import os, re, html, json, base64, mimetypes, datetime, argparse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get('FILE_PORTAL_ROOT', str(HOME))).expanduser().resolve()
PORT = int(os.environ.get('FILE_PORTAL_PORT', '8000'))
AUTH = os.environ.get('FILE_PORTAL_AUTH', '')  # "user:pass", empty = auth disabled

if ':' in AUTH:
    _AUTH_USER, _, _AUTH_PASS = AUTH.partition(':')
else:
    _AUTH_USER = _AUTH_PASS = None  # auth disabled


def _auth_ok(handler):
    if _AUTH_USER is None:
        return True
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Basic '):
        return False
    try:
        dec = base64.b64decode(auth[6:]).decode('utf-8', 'replace')
    except Exception:
        return False
    user, _, pw = dec.partition(':')
    return user == _AUTH_USER and pw == _AUTH_PASS


def _resolve(rel):
    """Resolve a client-supplied relative path inside ROOT. Returns None on escape."""
    p = (ROOT / rel).resolve()
    return p if (p == ROOT or ROOT in p.parents) else None


def _fmt_size(n):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_time(ts):
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')


INDEX_PAGE = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>File Portal</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;font-family:system-ui,sans-serif;background:#111;color:#eee;padding:24px;font-size:16px;display:flex;flex-direction:column;align-items:center}
h1{font-size:20px;margin:30px 0 6px;font-weight:600}
p{color:#888;font-size:14px;margin:0 0 28px}
a.btn{display:inline-block;background:#2f6fed;color:#fff;text-decoration:none;padding:12px 28px;border-radius:10px;font-size:15px}
a.btn:active{background:#4a85ff}
</style></head><body>
<h1>File Portal</h1>
<p>密码保护的个人文件管理</p>
<a class="btn" href="/files">打开文件管理</a>
</body></html>'''

LIST_PAGE = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>文件管理</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;font-family:system-ui,sans-serif;background:#111;color:#eee;padding:14px;font-size:16px}
h1{font-size:19px;margin:4px 0 10px;font-weight:600}
a.home{display:inline-block;color:#6ab0ff;text-decoration:none;font-size:14px;margin-bottom:8px}
.card{background:#1c1c1c;border:1px solid #333;border-radius:10px;padding:10px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 6px;border-bottom:1px solid #262626}
th{color:#888;font-weight:500;font-size:12px}
a{color:#6ab0ff;text-decoration:none}
a.dir{color:#e6b450}
.path{font-family:monospace;font-size:12px;color:#888;word-break:break-all}
input[type=file]{color:#ccc;max-width:100%}
button{background:#2f6fed;border:0;color:#fff;padding:10px 18px;border-radius:8px;cursor:pointer;font-size:14px}
button:active{background:#4a85ff}
.msg{margin-top:8px;font-size:13px;color:#3fb950}.err{color:#f85149}
</style></head><body>
<h1>文件管理</h1>
<a class="home" href="/">返回首页</a>
<div class="card"><div class="path">@PATH@</div></div>
<div class="card"><table>
<tr><th>名称</th><th>大小</th><th>修改时间</th></tr>
@ROWS@
</table></div>
<div class="card">
<form method="post" action="/upload?path=@QPATH@" enctype="multipart/form-data">
<input type="file" name="file"><button type="submit" style="margin-left:8px">上传到此目录</button>
</form>
<div class="msg" id="msg"></div>
</div>
<script>
document.querySelector('form').onsubmit=function(){document.getElementById('msg').textContent='上传中...';};
</script></body></html>'''


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _deny(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="file-portal"')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body):
        body = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_404(self, msg):
        body = msg.encode()
        self.send_response(404)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not _auth_ok(self):
            return self._deny()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == '/':
            return self._html(INDEX_PAGE)
        if u.path == '/files':
            rel = q.get('path', [''])[0]
            p = _resolve(rel)
            if p is None or not p.is_dir():
                return self._serve_404('目录不存在')
            return self._serve_listing(p, rel)
        if u.path == '/file':
            rel = q.get('path', [''])[0]
            p = _resolve(rel)
            if p is None or not p.is_file():
                return self._serve_404('文件不存在')
            return self._serve_file(p)
        self._serve_404('not found')

    def do_POST(self):
        if not _auth_ok(self):
            return self._deny()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == '/upload':
            rel = q.get('path', [''])[0]
            return self._upload(rel)
        self._json({'ok': False, 'msg': 'not found'}, 404)

    def _serve_listing(self, p, rel):
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return self._serve_404('无权限访问该目录')
        rows = []
        if rel:
            parent = rel.rstrip('/').rsplit('/', 1)[0]
            rows.append(f'<tr><td><a href="/files?path={html.escape(parent, quote=True)}">上级目录</a></td><td></td><td></td></tr>')
        for e in entries:
            if e.name.startswith('.'):
                continue
            name = html.escape(e.name)
            rel2 = (rel.rstrip('/') + '/' + e.name) if rel else e.name
            if e.is_dir():
                rows.append(f'<tr><td><a class="dir" href="/files?path={html.escape(rel2, quote=True)}">{name}/</a></td><td>-</td><td>{_fmt_time(e.stat().st_mtime)}</td></tr>')
            else:
                try:
                    sz = _fmt_size(e.stat().st_size)
                except OSError:
                    sz = '?'
                rows.append(f'<tr><td><a href="/file?path={html.escape(rel2, quote=True)}">{name}</a></td><td>{sz}</td><td>{_fmt_time(e.stat().st_mtime)}</td></tr>')
        body = (LIST_PAGE
                .replace('@ROWS@', '\n'.join(rows))
                .replace('@PATH@', html.escape(str(p)))
                .replace('@QPATH@', html.escape(rel, quote=True)))
        self._html(body)

    def _serve_file(self, p):
        try:
            sz = p.stat().st_size
        except OSError:
            return self._serve_404('文件不可读')
        mime = mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(sz))
        ascii_name = p.name.encode('ascii', 'replace').decode('ascii') or 'download'
        self.send_header('Content-Disposition',
                         f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(p.name)}")
        self.end_headers()
        with open(p, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _upload(self, rel):
        p = _resolve(rel)
        if p is None or not p.is_dir():
            return self._json({'ok': False, 'msg': '目标目录无效'})
        ct = self.headers.get('Content-Type', '')
        m = re.search(r'boundary=([^;]+)', ct)
        if not m:
            return self._json({'ok': False, 'msg': '非 multipart 请求'})
        boundary = m.group(1).strip('"').encode()
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length <= 0:
            return self._json({'ok': False, 'msg': '空请求'})
        body = self.rfile.read(length)
        fm = re.search(rb'filename="([^"]+)"', body[:8192])
        if not fm:
            return self._json({'ok': False, 'msg': '未找到文件字段'})
        fname = fm.group(1).decode('utf-8', 'replace')
        fname = os.path.basename(fname)
        marker = b'\r\n\r\n'
        head_end = body.find(marker)
        if head_end < 0:
            return self._json({'ok': False, 'msg': '格式错误'})
        data_start = head_end + 4
        data_end = body.rfind(b'\r\n--' + boundary)
        if data_end < 0:
            return self._json({'ok': False, 'msg': '格式错误'})
        data = body[data_start:data_end]
        dest = p / fname
        if dest.exists():
            base, ext = os.path.splitext(fname)
            i = 1
            while (p / f"{base}_{i}{ext}").exists():
                i += 1
            dest = p / f"{base}_{i}{ext}"
        with open(dest, 'wb') as f:
            f.write(data)
        return self._json({'ok': True, 'msg': f'已上传 {dest.name}（{_fmt_size(len(data))}）到 {rel or "根目录"}'})


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='File Portal: password-protected web file manager')
    ap.add_argument('--port', type=int, default=PORT, help=f'listen port (default {PORT}, env FILE_PORTAL_PORT)')
    ap.add_argument('--root', default=str(ROOT), help=f'exposed root dir (default $HOME, env FILE_PORTAL_ROOT)')
    ap.add_argument('--auth', default=AUTH, help='"user:pass" (default env FILE_PORTAL_AUTH; empty = no auth)')
    ap.add_argument('--host', default='127.0.0.1', help='bind address (default 127.0.0.1)')
    a = ap.parse_args()
    ROOT = Path(a.root).expanduser().resolve()
    AUTH = a.auth
    if ':' in AUTH:
        _AUTH_USER, _, _AUTH_PASS = AUTH.partition(':')
    else:
        _AUTH_USER = _AUTH_PASS = None
    print(f'file-portal: http://{a.host}:{a.port}  root={ROOT}  auth={"on" if _AUTH_USER else "OFF"}', flush=True)
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
