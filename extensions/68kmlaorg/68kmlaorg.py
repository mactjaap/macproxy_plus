import logging
import re
import io
from flask import request, Response
from bs4 import BeautifulSoup, Comment, NavigableString
from PIL import Image
import requests

# --- CONFIGURATION ---
ENABLE_DEBUG = True
ENABLE_IMAGES = True
DOMAIN = "68kmla.org"
USERNAME = None
SESSION = requests.Session()
ALLOWED_TAGS = {
    'html', 'head', 'title', 'body', 'center', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'p', 'ul', 'li', 'a', 'br', 'form', 'input', 'textarea', 'select', 'option', 'button', 'img', 'b'
}

# --- LOGGING SETUP ---
logger = logging.getLogger("68kmlaorg")
logger.setLevel(logging.DEBUG if ENABLE_DEBUG else logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG if ENABLE_DEBUG else logging.INFO)
ch.setFormatter(logging.Formatter('[68kMLA] %(levelname)s: %(message)s'))
logger.addHandler(ch)

# --- COMMON HELPERS ---
def get_user_agent(req):
    return {'User-Agent': req.headers.get('User-Agent', '')}

def get_username():
    global USERNAME
    if USERNAME is None:
        try:
            url = f"https://{DOMAIN}/bb/index.php?account/"
            r = SESSION.get(url, headers=get_user_agent(request))
            soup = BeautifulSoup(r.text, "html.parser")
            a = soup.find('a', class_='p-navgroup-link--user')
            USERNAME = a['title'] if a and a.has_attr('title') else None
            logger.debug("Detected username: %s", USERNAME)
        except Exception as e:
            logger.debug("get_username failed: %r", e)
            USERNAME = None
    return USERNAME

def clean_empty_lines(s):
    return "\n".join(line for line in s.splitlines() if line.strip())

def wrap_html2(inner, title, debug=""):
    dbg = f"<p style='color:red'>{debug}</p>" if ENABLE_DEBUG and debug else ""
    user = get_username()
    lg = f"<p>Logged in as {user}</p>" if user else ""
    nav = "<h3>[   ----------   68kMLA navigation menu  ----------   ] </h3>"
    ftr = (
        "<hr>\n<p>&copy; 2025 68kMLA -- "
        '<a href="#top">Top</a> | '
        '<a href="/bb/index.php?help/">Help</a> | '
        '<a href="/bb/index.php?misc/contact">Contact</a>'
        "</p>"
    )
    html = (
        f"<html><head><title>{title}</title></head><body>{dbg}{lg}{nav}{inner}{ftr}</body></html>"
    )
    return clean_empty_lines(html.replace('\u00A0', ' '))

def reencode_image(content):
    try:
        img = Image.open(io.BytesIO(content))
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img.convert('RGBA'), mask=img.convert('RGBA').split()[-1])
            img = bg
        else:
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, 'JPEG', progressive=False)
        return buf.getvalue(), 'image/jpeg'
    except Exception as e:
        logger.debug("Image re-encode failed: %r", e)
        return content, 'image/jpeg'

def strip_to_html2(html):
    soup = BeautifulSoup(html.replace('\u00A0', ' '), "html.parser")

    for img in soup.find_all("img", src=lambda v: v and v.startswith("data:")):
        img.decompose()

    for tag in soup.find_all(attrs={"style": True}):
        if "data:image" in tag["style"]:
            logger.debug("Removing inline style with data:image")
            del tag["style"]

    for style_tag in soup.find_all("style"):
        if style_tag.string and "data:image" in style_tag.string:
            logger.debug("Removing <style> block with data:image")
            style_tag.decompose()

    # [Keep the rest of your cleaning rules here as needed]

    return str(soup)

def handle_request(req):
    try:
        full = req.full_path
        path = req.path.lstrip('/')
        qs = req.query_string.decode('utf-8')
        debug = ""
        if ENABLE_DEBUG:
            debug = (
                f"<b>Proxy Request:</b><br>"
                f"Method: {req.method}<br>"
                f"Path:   {req.path}<br>"
                f"Headers:{dict(req.headers)}<br><br>"
            )
        logger.debug("Handling %s %s", req.method, req.full_path)

        url = f"https://{DOMAIN}{full}"
        headers = get_user_agent(req)

        if req.method == 'GET' and ('attachments/' in full or '/data/avatars/' in full or '/data/assets/' in full):
            r = SESSION.get(url)
            img_bytes, out_ct = reencode_image(r.content) if r.status_code == 200 and 'image/' in r.headers.get('Content-Type', '').lower() else (r.content, r.headers.get('Content-Type', ''))
            return Response(img_bytes, status=200, headers={'Content-Type': out_ct, 'Cache-Control': 'no-cache, no-store, must-revalidate', 'Pragma': 'no-cache', 'Expires': '0'}, direct_passthrough=True)

        r = SESSION.get(url, headers=headers)
        inner = strip_to_html2(r.text)
        title_tag = BeautifulSoup(r.text, 'html.parser').title
        title = title_tag.string if title_tag else '68kMLA'
        return wrap_html2(inner, title, debug), 200

    except Exception as e:
        logger.error("Exception in handle_request: %r", e)
        inner = (
            "<h1>500 – Internal Server Error</h1>"
            f"<p>{e}</p>"
            "<p><a href=\"/bb/index.php\">Return to 68kMLA Home</a></p>"
        )
        return wrap_html2(inner, "Error – Internal Server Error", ""), 500

