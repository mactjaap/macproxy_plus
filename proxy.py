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
session = requests.Session()

# ─── GLOBALS & CONFIG ────────────────────────────────────────────────────────
HTTP_ERRORS        = (403, 404, 500, 503, 504)
ERROR_HEADER       = "[[Macproxy Encountered an Error]]"
override_extension = None

# default user_agent
# USER_AGENT         = "MacProxyPlus/1.0 (+https://github.com/hunterirving/macproxy_plus) fork (https://github.com/mactjaap/macproxy_plus)"
# for better working on some sites
USER_AGENT	    = "Lynx/2.9.0dev.12 libwww-FM/2.14 SSL-MM/1.4.1 GNUTLS/3.7.8"

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

# ─── MAIN ROUTE ──────────────────────────────────────────────────────────────
@app.route('/', defaults={'path': '/'}, methods=['GET','POST'])
@app.route('/<path:path>', methods=['GET','POST'])
def handle_request(path):
    global override_extension

    # 1) Override extension?
    if override_extension:
        resp = handle_override_extension(request)
        if resp is not None:
            return process_response(resp, request.url)

    # 2) Domain‐specific extension?
    host = urlparse(request.url).netloc.split(':')[0]
    module = find_matching_extension(host)
    if module:
        return process_response(handle_matching_extension(module), request.url)

    # 3) Fallback: default proxy (includes images)
    return handle_default_request()

def handle_override_extension(req):
    global override_extension
    name = override_extension.split('.')[-1]
    if name in extensions:
        resp = extensions[name].handle_request(req)
        if hasattr(extensions[name], 'get_override_status') and not extensions[name].get_override_status():
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



# ─── PROCESS RESPONSE ────────────────────────────────────────────────────────
def process_response(response, url):
    """
    This function expects `response` to be a tuple (content_bytes, status_code, headers_dict),
    or a Flask Response/Werkzeug Response. It always returns a Flask Response object.
    """

    # — Step 1: Normalize `response` into (content_bytes, status, headers_dict) —
    if isinstance(response, tuple):
        if len(response) == 3:
            content_bytes, status, headers = response
        elif len(response) == 2:
            content_bytes, status = response
            headers = {}
        else:
            content_bytes = response[0]
            status = 200
            headers = {}
    elif isinstance(response, (Response, WerkzeugResponse)):
        # If an extension already returned a fully formed Flask Response, pass it straight through
        return response
    else:
        # Something else – treat as text, 200 OK
        content_bytes = response if isinstance(response, bytes) else str(response).encode('utf-8', 'replace')
        status = 200
        headers = {}

    ctype = headers.get('Content-Type', '').lower()
    app.logger.debug(f"Processing response for {url} → Content-Type: {ctype}")

    # — Step 2: If this is an image, re-encode/resize/convert via PIL —
    if ctype.startswith('image/'):
        data = content_bytes
        img_bytes = data
        out_ct    = ctype

        try:
            img = Image.open(io.BytesIO(data))
            app.logger.debug("PIL opened image: format=%s mode=%s size=%s", img.format, img.mode, img.size)

            # (2a) Flatten alpha onto white if needed
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                app.logger.debug("Image has transparency; flattening onto white")
                bg = Image.new('RGB', img.size, (255, 255, 255))
                rgba = img.convert('RGBA')
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            else:
                img = img.convert('RGB')

            # (2b) Resize if requested by your config
            if getattr(config, "RESIZE_IMAGES", False):
                orig_w, orig_h = img.size
                max_w = getattr(config, "MAX_IMAGE_WIDTH", orig_w)
                max_h = getattr(config, "MAX_IMAGE_HEIGHT", orig_h)
                scale = min(max_w / orig_w, max_h / orig_h, 1.0)
                if scale < 1.0:
                    new_size = (int(orig_w * scale), int(orig_h * scale))
                    img = img.resize(new_size, Image.LANCZOS)
                    app.logger.debug("Resized image to %s", new_size)

            # (2c) Convert to GIF if requested; otherwise JPEG
            if getattr(config, "CONVERT_IMAGES", False) and \
               getattr(config, "CONVERT_IMAGES_TO_FILETYPE", "").lower() == "gif":
                buf = io.BytesIO()
                algo_name = getattr(config, "DITHERING_ALGORITHM", "FLOYDSTEINBERG").upper()
                dither_const = getattr(Image, algo_name, Image.FLOYDSTEINBERG)
                img.convert('P', dither=dither_const).save(buf, 'GIF')
                img_bytes = buf.getvalue()
                out_ct    = 'image/gif'
                app.logger.debug("Converted image to GIF (dither=%s), size=%d", algo_name, len(img_bytes))
            else:
                buf = io.BytesIO()
                img.save(buf, 'JPEG', progressive=False)
                img_bytes = buf.getvalue()
                out_ct    = 'image/jpeg'
                app.logger.debug("Re-encoded image to JPEG, size=%d", len(img_bytes))

        except Exception as e:
            app.logger.debug("PIL failed for image; returning raw data: %r", e)
            img_bytes = data
            out_ct    = ctype

        resp = Response(img_bytes, status)
        resp.headers['Content-Type'] = out_ct
        resp.headers.update({
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma':        'no-cache',
            'Expires':       '0',
        })
        return resp

    # — Step 3: If CSS/JS, run transcode_content (string→string), then wrap as bytes —
    if ctype in ('text/css', 'text/javascript', 'application/javascript'):
        try:
            decoded = content_bytes.decode('utf-8', 'replace')
            txt = transcode_content(decoded)
            final_bytes = txt.encode('utf-8', 'replace')
            r = Response(final_bytes, status)
            r.headers['Content-Type'] = ctype
            return r
        except Exception as e:
            app.logger.debug("transcode_content failed: %r", e)
            # Fall back to raw
            r = Response(content_bytes, status)
            r.headers['Content-Type'] = ctype
            return r

    # — Step 4: HTML rewriting for anything that “looks like HTML/text” —
    non_transcode = (
        'application/octet-stream', 'application/pdf', 'application/zip',
        'audio/', 'video/', 'text/plain'
    )
    final_response_content = content_bytes  # default if no HTML rewriting applies

    if ctype.startswith('text/html') or not any(ctype.startswith(n) for n in non_transcode):
        # (4a) Decode bytes → str once
        if isinstance(content_bytes, bytes):
            html_content_str = content_bytes.decode('utf-8', 'replace')
        else:
            # In case an extension gave us a str instead
            html_content_str = content_bytes

        # (4b) Strip any leading <!doctype …> tag (first occurrence, case-insensitive)
        html_content_str = re.sub(r'(?i)<!doctype.*?>', '', html_content_str, count=1)

        # (4c) Strip any <!DOCTYPE …> declarations entirely
        html_content_str = re.sub(r'(?i)<!DOCTYPE[^>]*>\s*', '', html_content_str)

        # (4d) Collapse any “long” <html …> down to exactly "<html>"
        html_content_str = re.sub(r'(?i)<html\b[^>]*>', '<html>', html_content_str)

        # (4e) Hand off to your existing transcode_html pipeline
        try:
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
        except Exception as e:
            app.logger.debug("transcode_html failed: %r", e)
            final_transcoded = html_content_str

        # (4f) Ensure we return bytes, not str
        if isinstance(final_transcoded, bytes):
            final_response_content = final_transcoded
        else:
            final_response_content = final_transcoded.encode('utf-8', 'replace')

    # — Step 5: Build the final Flask response —
    resp = Response(final_response_content, status)
    for k, v in headers.items():
        lower = k.lower()
        # Skip hop-by-hop or encoding headers
        if lower not in (
            'content-encoding', 'content-length', 'transfer-encoding',
            'connection', 'proxy-authenticate', 'proxy-authorization'
        ):
            resp.headers[k] = v

    # Remove Transfer-Encoding if upstream set it
    if 'Transfer-Encoding' in resp.headers:
        del resp.headers['Transfer-Encoding']

    return resp






















# ── END HTML REWRITING ────────────────────────────────────────────────────



# ─── DEFAULT PROXY ──────────────────────────────────────────────────────────
def handle_default_request():
    upstream = request.url.replace('https://', 'http://', 1)
    try:
        r = session.request(
            method=request.method,
            url=upstream,
            params=request.args,
            data=request.form if request.method == 'POST' else None,
            headers=prepare_headers(),
            allow_redirects=True
        )
        return process_response((r.content, r.status_code, dict(r.headers)), request.url)
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
    # flatten transparency
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





# ─── OPTIONAL: LIGHT-BLUE BG FOR 68kmla.org ─────────────────────────────────
@app.after_request
def inject_body_bgcolor(resp: Response):
    ct   = resp.headers.get("Content-Type","")
    if not ct.startswith("text/html"):
        return resp
    host = urlparse(request.url).netloc.split(":",1)[0]
    if host != "68kmla.org":
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
