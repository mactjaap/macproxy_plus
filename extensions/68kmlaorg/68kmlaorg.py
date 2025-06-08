#!/usr/bin/env python3
# 68kmlaorg.py

import re
import io
import logging
import requests
from flask import Response, request
from bs4 import BeautifulSoup, Comment, NavigableString
from PIL import Image
import config
import urllib.parse

# Parse config values (strings "True"/"False") into booleans
ENABLE_DEBUG  = str(config.ENABLE_DEBUG).lower()  in ("1","true","yes")
ENABLE_IMAGES = str(config.ENABLE_IMAGES).lower() in ("1","true","yes")

SESSION = requests.Session()
DOMAIN  = "68kmla.org"
USERNAME = None

# HTML 2.0–legal tags whitelist
ALLOWED_TAGS = {
    'html','head','title',
    'body','center',
    'h1','h2','h3','h4','h5','h6',
    'p','ul','li','a','br','hr','pre','code',
    'form','input','textarea','select','option','button',
    'img','b'
}

# ─── Logging Setup ───────────────────────────────────────────────────────────
logger = logging.getLogger("68kmlaorg")
logger.setLevel(logging.DEBUG if ENABLE_DEBUG else logging.INFO)

# Try journald
try:
    from systemd.journal import JournalHandler as JournaldLogHandler
except ImportError:
    JournaldLogHandler = None

if JournaldLogHandler:
    jh = JournaldLogHandler()
    jh.setLevel(logging.DEBUG if ENABLE_DEBUG else logging.INFO)
    jh.setFormatter(logging.Formatter('[68kMLA] %(levelname)s: %(message)s'))
    logger.addHandler(jh)

# Fallback to stderr
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG if ENABLE_DEBUG else logging.INFO)
ch.setFormatter(logging.Formatter('[68kMLA] %(levelname)s: %(message)s'))
logger.addHandler(ch)


# ─── Helper: Fetch Username (zet USERNAME enkel voor weergave, maar login is uit) ─
def get_username():
    global USERNAME
    if USERNAME is None:
        try:
            url = f"https://{DOMAIN}/bb/index.php?account/"
            logger.debug("Fetching account page for username: %s", url)
            r = SESSION.get(url, headers={'User-Agent': request.headers.get('User-Agent','')})
            soup = BeautifulSoup(r.text, "html.parser")
            a = soup.find('a', class_='p-navgroup-link--user')
            USERNAME = a['title'] if a and a.has_attr('title') else None
            logger.debug("Detected username: %s", USERNAME)
        except Exception as e:
            logger.debug("get_username failed: %r", e)
            USERNAME = None
    return USERNAME


# ─── Strip & Rewrite to HTML 2.0 ──────────────────────────────────────────────
def strip_to_html2(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # ── REMOVE: any <title> tags accidentally carried into inner ─────────────
    for t in soup.find_all("title"):
        logger.debug("Removing stray <title> tag from inner HTML")
        t.decompose()

    # ── REMOVE: any data-template attribute on <body> ─────────────────────────
    body = soup.find("body")
    if body and "data-template" in body.attrs:
        logger.debug("Removing data-template attribute from <body>")
        del body["data-template"]

    # Remove all existing <hr>
    for hr in soup.find_all("hr"):
        hr.decompose()
    logger.debug("Removed all existing <hr> tags")

    # Remove inline base64 images
    for img in soup.find_all("img", src=lambda v: v and v.startswith("data:")):
        img.decompose()

    # Remove inline data: URLs in style attributes
    for tag in soup.find_all(attrs={"style": True}):
        if "data:image" in tag["style"]:
            logger.debug("Removed style with inline image")
            del tag["style"]

    # Remove standalone "Menu" label + close-button anchor
    for close_btn in soup.find_all("a", attrs={"data-menu-close": True}):
        prev = close_btn.previous_sibling
        if isinstance(prev, NavigableString) and "Menu" in prev:
            prev.extract()
        close_btn.decompose()
        logger.debug("Removed upstream 'Menu' label and close-button link")

    # Remove exact “What’s new” and “Search” nav links
    for a in soup.find_all("a", {
            "aria-label": "What's new",
            "href": re.compile(r"^/bb/index\.php\?whats-new/")
        }):
        a.decompose()
    for a in soup.find_all("a", {
            "aria-label": "Search",
            "href": re.compile(r"^/bb/index\.php\?search/")
        }):
        a.decompose()
    logger.debug("Removed upstream “What's new” and “Search” links")

    # ── REMOVE: any <ul> whose descendants have <a data-nav-id="…"> *unless* they also
    #    contain a <li data-xf-list-type="ul"> (i.e. content-lists)
    for ul in soup.find_all("ul"):
        has_nav_id = bool(ul.find("a", attrs={"data-nav-id": True}))
        has_xf_list = bool(ul.find("li", attrs={"data-xf-list-type": "ul"}))
        if has_nav_id and not has_xf_list:
            ul.decompose()
            logger.debug("Removed upstream XenForo menu <ul> (had data-nav-id, no data-xf-list-type)")

    # ── REMOVE: breadcrumb <ul> blocks with schema.org/BreadcrumbList ─────────
    for ul in soup.find_all("ul", attrs={"itemtype": "https://schema.org/BreadcrumbList"}):
        ul.decompose()
        logger.debug("Removed breadcrumb <ul> (schema.org/BreadcrumbList)")

    # ── REMOVE: conversations menu <ul> ───────────────────────────────────────
    for ul in soup.find_all("ul"):
        if ul.find("a", href=re.compile(r"^/bb/index\.php\?conversations/")):
            ul.decompose()
            logger.debug("Removed conversations <ul> (Show all / Start a new conversation)")

    # ── REMOVE logged-in “Conversations” block ─────────────────────────────────
    for h3 in soup.find_all("h3", string=lambda t: t and t.strip() == "Conversations"):
        next_ul = h3.find_next_sibling("ul")
        if next_ul:
            next_ul.decompose()
        h3.decompose()
        logger.debug("Removed logged-in Conversations section")

    # ── REMOVE: Alerts anchor, heading, and related <ul> ──────────────────────
    alerts_anchor = soup.find("a", attrs={"aria-label": "Alerts"})
    if alerts_anchor:
        alerts_anchor.decompose()
        logger.debug("Removed Alerts <a aria-label='Alerts'>")
    alerts_h3 = soup.find("h3", string="Alerts")
    if alerts_h3:
        alerts_h3.decompose()
        logger.debug("Removed <h3>Alerts</h3>")
        next_ul = alerts_h3.find_next_sibling("ul")
        if next_ul:
            next_ul.decompose()
            logger.debug("Removed <ul> onder Alerts heading")

    # Remove login/register links (want we hebben login uit)
    for a in soup.find_all("a", href=re.compile(r"/bb/index\.php\?login/|/bb/index\.php\?register/")):
        a.decompose()
    logger.debug("Removed upstream login/register links")

    # Insert custom navigation header zonder login/register
    custom_nav = BeautifulSoup('''
        <!-- Aangepaste menu -->
        <a href="/bb/index.php">Home</a> |
        <a href="/bb/index.php?forums/">Forums</a> |
        <a href="/bb/index.php?forums/68kmla-wiki.13/">Wiki</a> |
        <a href="/bb/index.php?whats-new/">What’s new</a> |
        <a href="/bb/index.php?media/">Media</a> |
        <a href="/bb/index.php?resources/">Resources</a> |
        <a href="/bb/index.php?members/">Members</a> |
        <a href="/forums/archive/">Snitz Archive</a> |
        <a href="https://www.patreon.com/68kmla">Patreon</a> |
        <a href="/bb/index.php?search/">Search</a>
        <br>
        <form>
            <label for="menu">Menu:</label>
            <select id="menu" onchange="window.location.href=this.value;">
                <option value="/bb/index.php">Home</option>
                <option value="/bb/index.php?forums/">Forums</option>
                <option value="/bb/index.php?whats-new/posts/">New posts</option>
                <option value="/bb/index.php?whats-new/media/">New media</option>
                <option value="/bb/index.php?whats-new/media-comments/">New media comments</option>
                <option value="/bb/index.php?whats-new/resources/">New resources</option>
                <option value="/bb/index.php?whats-new/profile-posts/">New profile posts</option>
                <option value="/bb/index.php?whats-new/latest-activity/">Latest activity</option>
                <option value="/bb/index.php?media/">Media</option>
                <option value="/bb/index.php?resources/">Resources</option>
                <option value="/bb/index.php?resources/latest-reviews">Resources latest reviews</option>
                <option value="/bb/index.php?members/">Members</option>
                <option value="/bb/index.php?online/">Current visitors</option>
                <option value="/forums/archive/">Snitz Archive</option>
                <option value="https://www.patreon.com/68kmla">Patreon</option>
                <option value="/bb/index.php?search/">Search</option>
                <option value="/forums/archive/">Snitz Archive</option>
                <option value="/bb/index.php?forums/68kmla-wiki.13/">Wiki</option>
            </select>
        </form>
        <hr>
        <!-- EINDE MENU -->
    ''', 'html.parser')

    body = soup.body or soup
    if body.contents:
        body.insert(0, custom_nav)
    else:
        body.append(custom_nav)
    logger.debug("Inserted custom navigation header")

    # Insert twee <br> na “Search” knop
    for btn2 in soup.find_all("button", {"type": "submit"}):
        txt = btn2.get_text(strip=True).lower()
        if txt in ("search", "find", "go"):
            logger.debug("Inserting <br><br> after '%s' knop", txt)
            btn2.insert_after(soup.new_tag("br"))
            btn2.insert_after(soup.new_tag("br"))

    # Vervang <button> met tekst “search”, “send”, “find”, “go” door <input type="submit">
    for btn in list(soup.find_all("button")):
        text = btn.get_text(strip=True).lower()
        if text in ("search", "send", "find", "go"):
            new_input = soup.new_tag("input", type="submit", value=text.capitalize())
            btn.insert_after(new_input)
            btn.decompose()
            logger.debug("Replaced <button>…</button> with <input type='submit' value='%s'>", text)

    # Remove XenForo “out of date browser” warning
    for warn in soup.find_all('div', class_=lambda c: c and 'js-browserWarning' in c):
        logger.debug("Removing browser-warning div")
        warn.decompose()

    # Replace “Loading…” tekst
    for txt in soup.find_all(string=lambda t: isinstance(t, str) and "Loading…" in t):
        logger.debug("Replacing 'Loading…' tekst")
        txt.replace_with(txt.replace("Loading…", ""))

    # Drop logo links (home buttons)
    for a in soup.find_all('a', href=re.compile(r'^https?://68kmla\.org/bb/?$')):
        if a.find('img', src=re.compile(r'/bb/data/assets/logo/')):
            logger.debug("Removing logo link")
            a.decompose()

    # Remove “Menu” knop
    for btn in soup.find_all('button', attrs={"aria-label": "Menu"}):
        logger.debug("Removing Menu knop")
        btn.decompose()

    # Rewrite attachment-preview links naar directe image URL
    for a in soup.find_all('a', href=re.compile(r'^/bb/index\.php\?attachments/')):
        old = a['href']
        img = a.find('img')
        if img and img.get('src'):
            new = img['src']
            a['href'] = new
            logger.debug("Rewrote attachment link %s → %s", old, new)


    # ── REWRITE absolute attachment-image URLs to proxy-relative paths ───────
    for img in soup.find_all('img', src=re.compile(r'^https?://68kmla\.org(/bb/data/attachments/.*)')):
        orig = img['src']
        parsed = urllib.parse.urlparse(orig)
        # turn “https://68kmla.org/bb/data/...jpg” into “/bb/data/...jpg”
        img['src'] = parsed.path
        logger.debug("Rewrote absolute image URL %s → %s", orig, img['src'])


    # ── ADJUST <img> TAG DIMENSIONS FOR OLD BROWSERS ───────────────────────────
    for img in soup.find_all("img"):
        src = img.get("src")
        # skip inline or missing
        if not src or src.startswith("data:"):
            continue
        try:
            # fetch the real image so we can measure it
            r = SESSION.get(src)
            im = Image.open(io.BytesIO(r.content))
            w, h = im.size
            # clamp to your maxs, preserve aspect
            max_w, max_h = 512, 342
            scale = min(max_w / w, max_h / h, 1)
            new_w, new_h = int(w * scale), int(h * scale)
            img["width"]  = str(new_w)
            img["height"] = str(new_h)
            logger.debug("Scaled <%s> from %dx%d to %dx%d", src, w, h, new_w, new_h)
        except Exception as e:
            logger.debug("Couldn't resize image %s: %r", src, e)


    # ── ADD spacing around every <img> so old browsers break lines correctly
    for img in soup.find_all("img"):
        # insert a <br> immediately before and after each image
        img.insert_before(soup.new_tag("br"))
        img.insert_after(soup.new_tag("br"))
        logger.debug("Wrapped <img> in <br> tags for spacing: %s", img.get("src"))



    # Voeg <hr> vóór elke <h1>
    for h1 in soup.find_all("h1"):
        h1.insert_before(soup.new_tag("hr"))
        logger.debug("Inserted <hr> before <h1>")

    # Voeg twee <br> vóór elke avatar link
    for a in soup.find_all('a'):
        img = a.find('img', src=re.compile(r'/bb/data/avatars/'))
        if img:
            a.insert_before(soup.new_tag("br"))
            a.insert_before(soup.new_tag("br"))
            logger.debug("Inserted <br><br> before avatar link to %s", a['href'])

    # Verwijder alle <script>, <style>, <link>, <noscript>, <svg> en comments
    for t in soup.find_all(['script','style','link','noscript','svg']):
        t.decompose()
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        c.extract()

    # Verwijder XenForo client-load-time hidden form
    for form in soup.find_all("form", hidden=True):
        if form.find("input", {"id": "_xfClientLoadTime"}):
            form.decompose()
            logger.debug("Removed hidden _xfClientLoadTime form")

    # Image stripping of PNG→JPEG re-encode (tags blijven; ophalen in proxy)
    if not ENABLE_IMAGES:
        for img in soup.find_all('img'):
            img.decompose()
    else:
        pass

    # Ontwrap tags die niet in HTML 2.0 mogen
    for tag in list(soup.find_all()):
        if tag.name.lower() not in ALLOWED_TAGS:
            tag.unwrap()

    # Voeg <hr><br> rond XenForo credit link
    for credit in soup.find_all("a", href=re.compile(r"https?://xenforo\.com"), rel=lambda v: v and "sponsored" in v):
        credit.insert_before(soup.new_tag("br"))
        credit.insert_before(soup.new_tag("br"))
        credit.insert_before(soup.new_tag("hr"))
        credit.insert_after(soup.new_tag("br"))
        logger.debug("Inserted <hr><br><br> after XenForo credit link")

    # Verwijder “Top” scroll-to link
    for a in soup.find_all("a", attrs={"data-xf-click": "scroll-to"}, string="Top"):
        a.decompose()
        logger.debug("Removed 'Top' scroll-to link")

    # Verwijder “Install the app” blok
    for install_button in soup.find_all("button", {"type": "button"}):
        if install_button.get_text(strip=True) == "Install":
            prev = install_button.find_previous_sibling()
            if prev and prev.name == "hr":
                prev.decompose()
            txt = install_button.previous_sibling
            if isinstance(txt, NavigableString) and "Install the app" in txt:
                txt.extract()
            install_button.decompose()
            logger.debug("Removed Install-the-app block")

    # Verwijder standalone “Install the app” tekst nodes
    for txt in soup.find_all(string=lambda s: isinstance(s, NavigableString) and "Install the app" in s):
        logger.debug('Removing text node containing "Install the app"')
        txt.replace_with(txt.replace("Install the app", ""))

    # Strip ongewenste attributes & stel eigen <body> in
    body = soup.find("body")
    if body and "data-template" in body.attrs:
        logger.debug("Removing data-template attribute from <body>")
        del body["data-template"]

    return str(soup)


# ─── Remove empty lines helper ───────────────────────────────────────────────
def clean_empty_lines(s: str) -> str:
    return "\n".join(line for line in s.splitlines() if line.strip())


# ─── Wrap into minimal HTML 2.0 skeleton ────────────────────────────────────
def wrap_html2(inner: str, title: str, debug: str = "", user_id: str = None) -> str:
    dbg = f"<p style='color:red'>{debug}</p>" if ENABLE_DEBUG and debug else ""

    snippet = inner[:200].replace("\n", " ").replace("\r", " ")
    logger.debug("wrap_html2: inner snippet (first 200 chars): %r", snippet)

    if user_id:
        logger.debug("wrap_html2: Received user_id → %r", user_id)
    else:
        logger.debug("wrap_html2: user_id was None")

    user = get_username()
    if user:
        lg = f"""
        <hr>
        <form>
            <label for="menu">Personal menu:</label>
            <select id="menu" onchange="window.location.href=this.value;">
                <option value="/bb/index.php">{user}</option>
                <option value="/bb/index.php?whats-new/news-feed/">News feed</option>
                <option value="/bb/index.php?search/member&user_id={user_id}">Your content</option>
                <option value="/bb/index.php?account/account-details">Account details</option>
                <option value="">-------------</option>
                <option value="/bb/index.php?whats-new/news-feed/">News feed</option>
                <option value="/bb/index.php?whats-new/media/">New media</option>
                <option value="/bb/index.php?whats-new/media-comments/">New media comments</option>
                <option value="/bb/index.php?whats-new/resources/">New resources</option>
                <option value="/bb/index.php?whats-new/profile-posts/">New profile posts</option>
                <option value="/bb/index.php?whats-new/latest-activity/">Latest activity</option>
                <option value="/bb/index.php?media/">Media</option>
                <option value="/bb/index.php?resources/">Resources</option>
                <option value="/bb/index.php?resources/latest-reviews">Resources latest reviews</option>
                <option value="/bb/index.php?members/">Members</option>
                <option value="/bb/index.php?online/">Current visitors</option>
                <option value="/bb/index.php?account/">Account</option>
                <option value="/bb/index.php?conversations/">Conversations</option>
                <option value="/bb/index.php?conversations/add">Start conversation</option>
                <option value="/bb/index.php?account/alerts">Alerts</option>
                <option value="/bb/index.php?account/preferences">Preferences</option>
                <option value="/bb/index.php?search/">Search</option>
            </select>
        </form>
        <br>
        """
    else:
        lg = ""

    nav = "\n"
    ftr = "<hr>\n"

    html = (
        "<html><head>\n"
        f"  <title>{title}</title>\n"
        "</head>\n"
        "<body TEMP_BODY>\n"
        f"{dbg}{lg}{nav}{inner}{ftr}"
        "</body></html>\n"
    )
    html = re.sub(r"<body[^>]*>", '<body bgcolor="lightblue">', html, count=1)
    return clean_empty_lines(html)


# ─── Search Flow ────────────────────────────────────────────────────────────
def _do_search(q: str, req, debug: str):
    form_url = f"https://{DOMAIN}/bb/index.php?search/"
    logger.debug("Search form URL: %s", form_url)
    r0 = SESSION.get(form_url, headers={'User-Agent': req.headers.get('User-Agent','')})
    if ENABLE_DEBUG:
        debug += (
            "<b>68kMLA Response:</b><br>"
            f"Status: {r0.status_code}<br>Headers: {dict(r0.headers)}<br><br>"
        )

    s0 = BeautifulSoup(r0.text, 'html.parser')
    xf = s0.find('input', {'name': '_xfToken'})
    data = {'keywords': q}
    if xf:
        data['_xfToken'] = xf['value']

    r1 = SESSION.post(
        f"https://{DOMAIN}/bb/index.php?search/search",
        headers={'User-Agent': req.headers.get('User-Agent',''), 'Referer': form_url},
        data=data,
        allow_redirects=False
    )
    if ENABLE_DEBUG:
        debug += (
            "<b>68kMLA Response:</b><br>"
            f"Status: {r1.status_code}<br>Headers: {dict(r1.headers)}<br><br>"
        )

    if r1.status_code in (301, 302, 303):
        loc = r1.headers.get('Location', '')
        if loc.startswith('/'):
            loc = f"https://{DOMAIN}{loc}"
        logger.debug("Redirecting search to %s", loc)
        r2 = SESSION.get(loc, headers={'User-Agent': req.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r2.status_code}<br>Headers: {dict(r2.headers)}<br><br>"
            )
        orig_soup = BeautifulSoup(r2.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        if span:
            user_id = span["data-user-id"]
        else:
            user_id = None

        inner = strip_to_html2(r2.text)
        title = (BeautifulSoup(r2.text, 'html.parser').title or f"Search: {q}").string
        return wrap_html2(inner, title, debug, user_id), 200

    orig_soup = BeautifulSoup(r1.text, "html.parser")
    span = orig_soup.find("span", attrs={"data-user-id": True})
    if span:
        user_id = span["data-user-id"]
    else:
        user_id = None

    inner = strip_to_html2(r1.text)
    title = (BeautifulSoup(r1.text, 'html.parser').title or f"Search: {q}").string
    return wrap_html2(inner, title, debug, user_id), r1.status_code


# ─── Main Entry Point ────────────────────────────────────────────────────────
def handle_request(req):
    full = req.full_path            # e.g. "/forums/archive/mainforums.jpg"
    path = req.path.lstrip('/')     # e.g. "forums/archive/mainforums.jpg"
    qs   = req.query_string.decode('utf-8')
    debug = ""
    if ENABLE_DEBUG:
        debug = (
            f"<b>Proxy Request:</b><br>"
            f"Method: {req.method}<br>"
            f"Path:   {req.path}<br>"
            f"Headers:{dict(req.headers)}<br><br>"
        )
    logger.debug("Handling %s %s", req.method, req.full_path)

    # ─── 1) attachments → binary + PIL re-encode ─────────────────────────────────
    if req.method == 'GET' and 'attachments/' in full:
        url = f"https://{DOMAIN}{full}"
        logger.debug("Fetching attachment: %s", url)
        r = SESSION.get(url)
        orig_ct = r.headers.get('Content-Type','').lower()
        logger.debug("Attachment response: %d bytes @ %s", len(r.content), orig_ct)

        img_bytes = r.content
        out_ct    = orig_ct

        if r.status_code == 200 and orig_ct.startswith('image/'):
            try:
                img = Image.open(io.BytesIO(r.content))
                logger.debug("PIL opened attachment: format=%s mode=%s size=%s",
                             img.format, img.mode, img.size)
                if img.mode in ('RGBA','LA') or (img.mode == 'P' and 'transparency' in img.info):
                    logger.debug("Attachment has transparency, compositing on white")
                    bg = Image.new('RGB', img.size, (255,255,255))
                    rgba = img.convert('RGBA')
                    bg.paste(rgba, mask=rgba.split()[-1])
                    img = bg
                else:
                    img = img.convert('RGB')

                buf = io.BytesIO()
                img.save(buf, 'JPEG', progressive=False)
                img_bytes = buf.getvalue()
                out_ct    = 'image/jpeg'
                logger.debug("Re-encoded attachment to JPEG, new size=%d", len(img_bytes))
            except Exception as e:
                logger.debug("PIL failed for attachment, sending raw: %r", e)

        return Response(
            img_bytes,
            status=200,
            headers={
                'Content-Type':   out_ct,
                'Cache-Control':  'no-cache, no-store, must-revalidate',
                'Pragma':         'no-cache',
                'Expires':        '0'
            },
            direct_passthrough=True
        )

    # ─── 2) UNIVERSAL IMAGE CATCH ───────────────────────────────────────────────
    if req.method == 'GET' and re.search(r'\.(?:jpe?g|png|gif|bmp|webp|svg|ico)(?:[?#]|$)', full.lower()):
        url = f"https://{DOMAIN}{full}"
        logger.debug("Generic image catch: %s", url)
        r = SESSION.get(url, headers={'User-Agent': req.headers.get('User-Agent','')})
        orig_ct = r.headers.get('Content-Type','').lower()

        try:
            buf = io.BytesIO(r.content)
            img = Image.open(buf)
            if img.mode in ('RGBA','LA') or (img.mode == 'P' and 'transparency' in img.info):
                bg = Image.new('RGB', img.size, (255,255,255))
                bg.paste(img.convert('RGBA'), mask=img.convert('RGBA').split()[-1])
                img = bg
            else:
                img = img.convert('RGB')

            out = io.BytesIO()
            img.save(out, 'JPEG', progressive=False)
            img_bytes, out_ct = out.getvalue(), 'image/jpeg'
            logger.debug("PIL re-encode succeeded: %s → image/jpeg", full)
        except Exception as e:
            logger.debug("PIL failed (%r), falling back to orig bytes", e)
            img_bytes, out_ct = r.content, orig_ct

        return Response(
            img_bytes,
            status=200,
            headers={
                'Content-Type':   out_ct,
                'Cache-Control':  'no-cache, no-store, must-revalidate',
                'Pragma':         'no-cache',
                'Expires':        '0',
            },
            direct_passthrough=True
        )

    # ─── 3) Direct Snitz Archive ─────────────────────────────────────────────────
    if req.method == 'GET' and path.startswith('forums/archive'):
        url = f"https://{DOMAIN}/{path}"
        logger.debug("Fetching Snitz Archive: %s", url)
        r = SESSION.get(url, headers={'User-Agent': req.headers.get('User-Agent','')})
        orig_soup = BeautifulSoup(r.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        if span:
            user_id = span["data-user-id"]
        else:
            user_id = None

        title_tag = orig_soup.find('title')
        title = title_tag.string if title_tag else "Snitz Archive"
        inner = strip_to_html2(r.text)
        return wrap_html2(inner, title, debug, user_id), 200

    # ─── 4) Direct search-ID GET ───────────────────────────────────────────────
    if req.method == 'GET' and path.endswith('index.php') and 'search/' in qs:
        url = f"https://{DOMAIN}/{path}?{qs}"
        logger.debug("Direct search GET: %s", url)
        r = SESSION.get(url, headers={'User-Agent': req.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r.status_code}<br>Headers: {dict(r.headers)}<br><br>"
            )

        orig_soup = BeautifulSoup(r.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        if span:
            user_id = span["data-user-id"]
        else:
            user_id = None

        inner = strip_to_html2(r.text)
        title = (BeautifulSoup(r.text, 'html.parser').title or 'Search').string
        return wrap_html2(inner, title, debug, user_id), 200

    # ─── 5) Quick-search POST ─────────────────────────────────────────────────
    if req.method == 'POST' and 'search/search' in full:
        q = req.form.get('keywords', '').strip()
        if not q:
            return wrap_html2("<p>No search term</p>", "Error", debug, None), 400
        return _do_search(q, req, debug)

    # ─── 6) GET /search?q= ─────────────────────────────────────────────────────
    if req.method == 'GET' and path.rstrip('/') == 'search' and 'q' in req.args:
        q = req.args.get('q', '').strip()
        if not q:
            return wrap_html2("<p>No search term</p>", "Error", debug, None), 400
        return _do_search(q, req, debug)

    # ─── 7) Home page ─────────────────────────────────────────────────────────
    if (
        req.method == 'GET'
        and (not path or path in ['bb', 'bb/', 'bb/index.php'])
        and (not qs or qs.startswith('images='))
    ):
        url = f"https://{DOMAIN}/bb/index.php"
        logger.debug("Fetching home page: %s", url)
        r = SESSION.get(url, headers={'User-Agent': req.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r.status_code}<br>Headers: {dict(r.headers)}<br><br>"
            )

        orig_soup = BeautifulSoup(r.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        if span:
            user_id = span["data-user-id"]
        else:
            user_id = None

        inner = strip_to_html2(r.text)
        title = (BeautifulSoup(r.text, 'html.parser').title or '68kMLA Home').string
        return wrap_html2(inner, title, debug, user_id), 200

    # ─── 8) All other index.php pages ─────────────────────────────────────────
    if req.method == 'GET' and 'index.php' in path:
        url = f"https://{DOMAIN}/{path}" + (f'?{qs}' if qs else '')
        logger.debug("Fetching other page: %s", url)
        r = SESSION.get(url, headers={'User-Agent': req.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r.status_code}<br>Headers: {dict(r.headers)}<br><br>"
            )

        orig_soup = BeautifulSoup(r.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        if span:
            user_id = span["data-user-id"]
        else:
            user_id = None

        inner = strip_to_html2(r.text)
        title = (BeautifulSoup(r.text, 'html.parser').title or '68kMLA').string
        return wrap_html2(inner, title, debug, user_id), 200

    # ─── 9) Handle add-reply POSTs ───────────────────────────────────────────
    if req.method == 'POST' and 'add-reply' in full:
        url = f"https://{DOMAIN}{full}"
        logger.debug("Proxying add-reply POST to %s", url)
        r = SESSION.post(
            url,
            data=req.form,
            headers={'User-Agent': request.headers.get('User-Agent','')},
            allow_redirects=False
        )

        if r.status_code in (301, 302, 303):
            loc = r.headers.get('Location', '')
            if loc.startswith('/'):
                loc = f"https://{DOMAIN}{loc}"
            logger.debug("add-reply redirected to %s", loc)
            r2 = SESSION.get(loc, headers={'User-Agent': request.headers.get('User-Agent','')})
            orig_soup = BeautifulSoup(r2.text, "html.parser")
            span = orig_soup.find("span", attrs={"data-user-id": True})
            if span:
                user_id = span["data-user-id"]
            else:
                user_id = None

            inner = strip_to_html2(r2.text)
            title_tag = BeautifulSoup(r2.text, 'html.parser').title
            title = title_tag.string if title_tag else "Reply Posted"
            return wrap_html2(inner, title, debug, user_id), 200

        orig_soup = BeautifulSoup(r.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        if span:
            user_id = span["data-user-id"]
        else:
            user_id = None

        inner = strip_to_html2(r.text)
        title_tag = BeautifulSoup(r.text, 'html.parser').title
        title = title_tag.string if title_tag else "Reply Result"
        return wrap_html2(inner, title, debug, user_id), r.status_code

    # ─── Method Not Allowed (als we hier belanden) ─────────────────────────────
    logger.debug("Method not allowed: %s %s", req.method, req.full_path)
    inner = (
        "<h1>405 – Method Not Allowed</h1>"
        "<p>Sorry, deze proxy kan dat verzoek niet verwerken.</p>"
        "<p><a href=\"/bb/index.php\">Terug naar 68kMLA Home</a></p>"
    )
    return wrap_html2(inner, "Error – Method Not Allowed", debug, None), 405
