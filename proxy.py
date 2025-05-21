import argparse
import os
import shutil
import socket
from urllib.parse import urlparse

import requests
from flask import Flask, request, abort, Response, send_from_directory, g
from werkzeug.serving import get_interface_ip
from werkzeug.wrappers.response import Response as WerkzeugResponse

from utils.html_utils import transcode_html, transcode_content
from utils.image_utils import is_image_url, CACHE_DIR
from utils.system_utils import load_preset

# ─── FLASK & SESSION SETUP ─────────────────────────────────────────────────
os.environ['FLASK_ENV'] = 'development'
app = Flask(__name__)
session = requests.Session()

# ─── PROXY CONFIG ───────────────────────────────────────────────────────────
HTTP_ERRORS = (403, 404, 500, 503, 504)
ERROR_HEADER = "[[Macproxy Encountered an Error]]"
override_extension = None

# User-Agent string
USER_AGENT = "MacProxyPlus/1.0 (+https://github.com/hunterirving/macproxy_plus)"

# ─── CLEAR IMAGE CACHE ON START ─────────────────────────────────────────────
from utils.image_utils import fetch_and_cache_image

def clear_image_cache():
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
    os.makedirs(CACHE_DIR, exist_ok=True)

clear_image_cache()

# ─── LOAD PRESET & EXTENSIONS ───────────────────────────────────────────────
config = load_preset()
ENABLED_EXTENSIONS = config.ENABLED_EXTENSIONS

extensions = {}
domain_to_extension = {}
print('Enabled Extensions:')
for ext in ENABLED_EXTENSIONS:
    print('  -', ext)
    module = __import__(f"extensions.{ext}.{ext}", fromlist=[''])
    extensions[ext] = module
    domain_to_extension[module.DOMAIN] = module

# ─── ROUTE: SERVE CACHED IMAGES ─────────────────────────────────────────────
@app.route('/cached_image/<path:filename>')
def serve_cached_image(filename):
    return send_from_directory(CACHE_DIR, filename, mimetype='image/gif')

# ─── MAIN PROXY ENDPOINT ────────────────────────────────────────────────────
@app.route('/', defaults={'path': '/'}, methods=['GET', 'POST'])
@app.route('/<path:path>', methods=['GET', 'POST'])
def handle_request(path):
    global override_extension
    parsed = urlparse(request.url)
    host = parsed.netloc.split(':')[0]
    scheme = parsed.scheme

    # 1) Override extension in effect?
    if override_extension:
        name = override_extension.split('.')[-1]
        if name in extensions and scheme in ('http','https','ftp'):
            resp = extensions[name].handle_request(request)
            if hasattr(extensions[name], 'get_override_status') and not extensions[name].get_override_status():
                override_extension = None
            return process_response(resp, request.url)
        override_extension = None

    # 2) Domain-specific extension?
    if host in domain_to_extension:
        ext = domain_to_extension[host]
        resp = ext.handle_request(request)
        if hasattr(ext, 'get_override_status') and ext.get_override_status():
            override_extension = ext.__name__
        return process_response(resp, request.url)

    # 3) 68kmlaorg-style image handling (any image URL)
    from PIL import Image
    import io
    if request.method == 'GET' and is_image_url(request.url) and not override_extension:
        # fetch upstream
        r = session.get(request.url, headers=prepare_headers())
        orig_ct = r.headers.get('Content-Type','').lower()
        img_bytes = r.content
        out_ct = orig_ct
        try:
            img = Image.open(io.BytesIO(r.content))
            # flatten transparency
            if img.mode in ('RGBA','LA') or (img.mode=='P' and 'transparency' in img.info):
                bg = Image.new('RGB', img.size, (255,255,255))
                rgba = img.convert('RGBA')
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            else:
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, 'JPEG', progressive=False)
            img_bytes = buf.getvalue()
            out_ct = 'image/jpeg'
        except Exception:
            pass
        headers = {
            'Content-Type':  out_ct,
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma':        'no-cache',
            'Expires':       '0',
        }
        return Response(img_bytes, status=200, headers=headers, direct_passthrough=True)

    # 4) Default request handling
    return handle_default_request()

# ─── PROCESS A LOWER-LEVEL RESPONSE ─────────────────────────────────────────
def process_response(response, url):
    # normalize tuple or Response
    if isinstance(response, tuple):
        if len(response) == 3:
            content, status, hdrs = response
        elif len(response) == 2:
            content, status = response; hdrs = {}
        else:
            content = response[0]; status = 200; hdrs = {}
    elif isinstance(response, (Response, WerkzeugResponse)):
        return response
    else:
        content, status, hdrs = response, 200, {}

    ctype = hdrs.get('Content-Type','').lower()
    # inline image handling already done above
    # CSS/JS
    if ctype in ('text/css','text/javascript','application/javascript'):
        txt = transcode_content(content)
        resp = Response(txt, status)
        resp.headers['Content-Type'] = ctype
        return resp

    # non-transcode types
    non = (
        'application/octet-stream','application/pdf','application/zip',
        'audio/','video/','text/plain'
    )
    should = not any(ctype.startswith(n) for n in non)
    if should:
        if isinstance(content, bytes):
            content = content.decode('utf-8', errors='replace')
        content = transcode_html(
            content, url,
            whitelisted_domains=config.WHITELISTED_DOMAINS,
            simplify_html=config.SIMPLIFY_HTML,
            tags_to_unwrap=config.TAGS_TO_UNWRAP,
            tags_to_strip=config.TAGS_TO_STRIP,
            attributes_to_strip=config.ATTRIBUTES_TO_STRIP,
            convert_characters=config.CONVERT_CHARACTERS,
            conversion_table=config.CONVERSION_TABLE
        )
    resp = Response(content, status)
    for k,v in hdrs.items():
        if k.lower() not in ('content-encoding','content-length'):
            resp.headers[k] = v
    return resp

# ─── HANDLE FALLBACK / PROXY ─────────────────────────────────────────────────
def handle_default_request():
    url = request.url.replace('https://','http://',1)
    try:
        resp = session.request(
            method=request.method,
            url=url,
            params=request.args,
            data=request.form if request.method=='POST' else None,
            headers=prepare_headers(),
            allow_redirects=True
        )
        return (resp.content, resp.status_code, dict(resp.headers))
    except Exception as e:
        return abort(500, ERROR_HEADER + str(e))

# ─── UTILS: HEADERS & HOSTNAME ───────────────────────────────────────────────
def prepare_headers():
    return {
        'Accept':          request.headers.get('Accept'),
        'Accept-Language': request.headers.get('Accept-Language'),
        'Referer':         request.headers.get('Referer'),
        'User-Agent':      USER_AGENT,
    }

@app.after_request
def apply_caching(resp):
    try:
        resp.headers['Content-Type'] = g.content_type
    except:
        pass
    return resp

def get_proxy_hostname(bind):
    if bind == '0.0.0.0':
        return get_interface_ip(socket.AF_INET)
    if bind == '::':
        return get_interface_ip(socket.AF_INET6)
    return bind

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Macproxy command line arguments')
    parser.add_argument('--host', type=str, default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5001)
    args = parser.parse_args()

    app.config['MACPROXY_HOST_AND_PORT'] = f"{get_proxy_hostname(args.host)}:{args.port}"
    app.run(host=args.host, port=args.port, debug=False)

