#!/usr/bin/env python3
# proxy.py

import argparse
import io
import os
import re
import shutil
import socket
from urllib.parse import urlparse

import requests
from flask import Flask, request, abort, Response, send_from_directory
from werkzeug.serving import get_interface_ip
from werkzeug.wrappers import Response as WerkzeugResponse
from PIL import Image

from utils.html_utils import transcode_html, transcode_content
from utils.image_utils import is_image_url, fetch_and_cache_image, CACHE_DIR
from utils.system_utils import load_preset

# ─── APP SETUP ───────────────────────────────────────────────────────────────
os.environ['FLASK_ENV'] = 'development'
app = Flask(__name__)

# ─── GLOBALS & CONFIG ────────────────────────────────────────────────────────
HTTP_ERRORS        = (403, 404, 500, 503, 504)
ERROR_HEADER       = "[[Macproxy Encountered an Error]]"
override_extension = None

PROXY_DOMAIN       = "proxy.macip.net"
UPSTREAM_DOMAIN    = "68kmla.org"
USER_AGENT         = "MacProxyPlus/1.0 (+https://github.com/hunterirving/macproxy_plus) fork (https://github.com/mactjaap/macproxy_plus) - website proxy version"

# ─── CLEAR IMAGE CACHE ON START ──────────────────────────────────────────────
def clear_image_cache():
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
    os.makedirs(CACHE_DIR, exist_ok=True)

clear_image_cache()

# ─── LOAD PRESET & EXTENSIONS ────────────────────────────────────────────────
config = load_preset()
ENABLED_EXTENSIONS = config.ENABLED_EXTENSIONS

extensions = {}
domain_to_extension = {}
app.logger.info(f"Enabled Extensions: {ENABLED_EXTENSIONS}")
for ext in ENABLED_EXTENSIONS:
    module = __import__(f"extensions.{ext}.{ext}", fromlist=[''])
    extensions[ext] = module
    domain_to_extension[module.DOMAIN] = module

# ─── IMAGE-CACHE ENDPOINT ───────────────────────────────────────────────────
@app.route('/cached_image/<path:filename>')
def serve_cached_image(filename):
    return send_from_directory(CACHE_DIR, filename, mimetype='image/gif')

# ─── HELPERS VOOR EXTENSIONS ─────────────────────────────────────────────────
def handle_override_extension(req):
    global override_extension
    name = override_extension.split('.')[-1]
    if name in extensions:
        module = extensions[name]
        resp = module.handle_request(req)
        if hasattr(module, 'get_override_status') and not module.get_override_status():
            override_extension = None
        return resp
    override_extension = None
    return None

def find_matching_extension(host):
    for domain, module in domain_to_extension.items():
        if host.endswith(domain):
            return module
    return None

def handle_matching_extension(module):
    global override_extension
    resp = module.handle_request(request)
    if hasattr(module, 'get_override_status') and module.get_override_status():
        override_extension = module.__name__
    return resp

# ─── MAIN ROUTE ──────────────────────────────────────────────────────────────
@app.route('/', defaults={'path': ''}, methods=['GET','POST'])
@app.route('/<path:path>', methods=['GET','POST'])
def handle_request(path):
    global override_extension

    try:
        # 1) Override‐extension?
        if override_extension:
            resp = handle_override_extension(request)
            if resp is not None:
                return process_response(resp, request.url)

        # 2) Host‐check: is dit verzoek naar proxy.macip.net?
        host = request.host.split(':')[0]
        if host == PROXY_DOMAIN:
            # Blokkeer login‐pogingen door “login” in path
            if 'login' in path.lower():
                return Response(
                    "<html><body><h1>Login disabled</h1></body></html>",
                    403,
                    {"Content-Type": "text/html"}
                )
            # Anders: route via 68kmlaorg‐extensie
            if '68kmlaorg' in extensions:
                module = extensions['68kmlaorg']
                resp = handle_matching_extension(module)
                return process_response(resp, request.url)
            else:
                app.logger.error("68kmlaorg‐extensie is niet ingeladen maar proxy.macip.net kreeg een verzoek.")
                abort(500, ERROR_HEADER + " → 68kmlaorg‐extensie niet gevonden")

        # 3) Andere extension‐host (bv. reddit.com, wikipedia.org, etc.)
        module = find_matching_extension(host)
        if module:
            resp = handle_matching_extension(module)
            return process_response(resp, request.url)

        # 4) Fallback: alle andere hosts → standaard doorsturen naar 68kmla.org
        return handle_default_request()

    except Exception as e:
        app.logger.exception("Onverwachte fout in handle_request:")
        abort(500, ERROR_HEADER + str(e))

# ─── PROCESS RESPONSE ────────────────────────────────────────────────────────
def process_response(response, url):
    if isinstance(response, tuple):
        if len(response) == 3:
            content, status, headers = response
        elif len(response) == 2:
            content, status = response
            headers = {}
        else:
            content, status, headers = response[0], 200, {}
    elif isinstance(response, (Response, WerkzeugResponse)):
        return response
    else:
        content, status, headers = response, 200, {}

    # Verwijder eventuele Set-Cookie in response headers
    headers = {k: v for k, v in headers.items() if k.lower() != 'set-cookie'}

    ctype = headers.get('Content-Type', '').lower()
    app.logger.debug(f"Processing response voor {url} → Content-Type: {ctype}")

    # ── IMAGE HANDLING ───────────────────────────────────────────────────────
    if ctype.startswith('image/'):
        subtype = ctype.split('/', 1)[1].split(';', 1)[0]
        data = content
        if subtype == 'gif':
            img_bytes, out_ct = data, 'image/gif'
        else:
            try:
                img_bytes, out_ct = _reencode_image(data)
            except Exception as e:
                app.logger.debug(f"PIL re-encode failed voor {subtype}: {e}")
                img_bytes, out_ct = data, ctype
        resp = Response(img_bytes, status)
        resp.headers['Content-Type'] = out_ct
        resp.headers.update({
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma':        'no-cache',
            'Expires':       '0',
        })
        return resp

    # ── CSS/JS TRANSCODING ────────────────────────────────────────────────────
    if ctype in ('text/css', 'text/javascript', 'application/javascript'):
        decoded_content = content.decode('utf-8', errors='replace') if isinstance(content, (bytes, bytearray)) else str(content)
        txt = transcode_content(decoded_content)
        final_css = txt.encode('utf-8', errors='replace')
        r = Response(final_css, status)
        r.headers['Content-Type'] = ctype
        return r

    # ── HTML REWRITING ────────────────────────────────────────────────────────
    non_transcode = (
        'application/octet-stream', 'application/pdf', 'application/zip',
        'audio/', 'video/', 'text/plain'
    )
    if ctype.startswith('text/html') or not any(ctype.startswith(n) for n in non_transcode):
        if isinstance(content, (bytes, bytearray)):
            html_content_str = content.decode('utf-8', 'replace')
        else:
            html_content_str = str(content)

        html_content_str = re.sub(r'(?i)<!doctype.*?>', '', html_content_str, count=1)
        html_content_str = re.sub(r'(?i)<!DOCTYPE[^>]*>\s*', '', html_content_str)
        html_content_str = re.sub(r'(?i)<html\b[^>]*>', '<html>', html_content_str)

        final_transcoded = transcode_html(
            html_content_str, url,
            whitelisted_domains   = config.WHITELISTED_DOMAINS,
            simplify_html         = config.SIMPLIFY_HTML,
            tags_to_unwrap        = config.TAGS_TO_UNWRAP,
            tags_to_strip         = config.TAGS_TO_STRIP,
            attributes_to_strip   = config.ATTRIBUTES_TO_STRIP,
            convert_characters    = config.CONVERT_CHARACTERS,
            conversion_table      = config.CONVERSION_TABLE
        )

        try:
            html_str = final_transcoded if isinstance(final_transcoded, str) else final_transcoded.decode('utf-8', 'replace')
            html_str = re.sub(r'action="https?://68kmla\.org', 'action="', html_str)
            html_str = re.sub(r'href="https?://68kmla\.org', 'href="', html_str)
            final_transcoded = html_str
        except Exception:
            pass

        if isinstance(final_transcoded, (bytes, bytearray)):
            final_response_content = final_transcoded
        else:
            final_response_content = final_transcoded.encode('utf-8', errors='replace')
    else:
        final_response_content = content

    resp = Response(final_response_content, status)
    for k, v in headers.items():
        if k.lower() not in (
            'content-encoding',
            'content-length',
            'transfer-encoding',
            'connection',
            'proxy-authenticate',
            'proxy-authorization'
        ):
            resp.headers[k] = v
    if 'Transfer-Encoding' in resp.headers:
        del resp.headers['Transfer-Encoding']
    return resp

# ─── DEFAULT PROXY ──────────────────────────────────────────────────────────
def handle_default_request():
    path_only = request.path
    qs = request.query_string.decode('utf-8')
    upstream = f"https://{UPSTREAM_DOMAIN}{path_only}"
    if qs:
        upstream += "?" + qs

    # Route via extension voor alle andere paths
    if '68kmlaorg' in extensions:
        module = extensions['68kmlaorg']
        resp = handle_matching_extension(module)
        return process_response(resp, request.url)

    # Indien extensie ontbreekt, val terug op direct verzoek zonder cookies
    req_headers = prepare_headers()
    try:
        if request.method == 'POST':
            r = requests.post(upstream, data=request.form, headers=req_headers, allow_redirects=True)
        else:
            r = requests.get(upstream, params=request.args, headers=req_headers, allow_redirects=True)
        resp_headers = {k: v for k, v in r.headers.items() if k.lower() != 'set-cookie'}
        return process_response((r.content, r.status_code, resp_headers), upstream)
    except Exception as e:
        abort(500, ERROR_HEADER + str(e))

# ─── HEADER PREP ────────────────────────────────────────────────────────────
def prepare_headers():
    return {
        'Accept':          request.headers.get('Accept'),
        'Accept-Language': request.headers.get('Accept-Language'),
        'Referer':         request.headers.get('Referer'),
        'User-Agent':      USER_AGENT,
    }

# ─── IMAGE RE-ENCODE HELPER ─────────────────────────────────────────────────
def _reencode_image(data: bytes) -> tuple[bytes,str]:
    buff = io.BytesIO(data)
    img = Image.open(buff)
    if img.mode in ('RGBA','LA') or (img.mode=='P' and 'transparency' in img.info):
        bg = Image.new('RGB', img.size, (255,255,255))
        rgba = img.convert('RGBA')
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert('RGB')
    out = io.BytesIO()
    img.save(out, 'JPEG', progressive=False)
    return out.getvalue(), 'image/jpeg'

# ─── OPTIONAL: LIGHT-BLUE BG VOOR 68kmla.org ─────────────────────────────────
@app.after_request
def inject_body_bgcolor(resp: Response):
    ct   = resp.headers.get("Content-Type","")
    if not ct.startswith("text/html"):
        return resp
    host = urlparse(request.url).netloc.split(":",1)[0]
    if host != UPSTREAM_DOMAIN:
        return resp
    html = resp.get_data(as_text=True)
    new  = re.sub(r"<body[^>]*>", '<body bgcolor="#F2F8FD">', html, count=1, flags=re.IGNORECASE)
    resp.set_data(new)
    return resp

# ─── UTIL: GET PROXY HOSTNAME ───────────────────────────────────────────────
def get_proxy_hostname(bind: str) -> str:
    if bind == '0.0.0.0':
        return get_interface_ip(socket.AF_INET)
    if bind == '::':
        return get_interface_ip(socket.AF_INET6)
    return bind

# ─── RUN ────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Macproxy command line arguments')
    p.add_argument('--host', type=str, default='0.0.0.0')
    p.add_argument('--port', type=int, default=5001)
    args = p.parse_args()

    app.config['MACPROXY_HOST_AND_PORT'] = f"{get_proxy_hostname(args.host)}:{args.port}"
    app.run(host=args.host, port=args.port, debug=False)
