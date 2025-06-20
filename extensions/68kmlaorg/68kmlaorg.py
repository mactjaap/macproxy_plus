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

# for logout
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
    'img','b','i'
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


# ─── Helper: Fetch Username ───────────────────────────────────────────────────
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


# ─── Extract_logout_link──────────────────────────────────────────────

def extract_logout_link(html_content: str) -> str | None:
    soup_temp = BeautifulSoup(html_content, "html.parser")
    logout_link_tag = soup_temp.find("a", class_="menu-linkRow", string="Log out")
    if logout_link_tag and "href" in logout_link_tag.attrs:
        logout_href = logout_link_tag["href"]
        unescaped_logout_href = urllib.parse.unquote(logout_href)
        logger.debug("Extracted logout URL from direct link: %s", unescaped_logout_href)
        return unescaped_logout_href
    html_tag = soup_temp.find("html")
    if html_tag and "data-csrf" in html_tag.attrs:
        csrf_token = html_tag["data-csrf"]
        logout_url = f"/bb/index.php?logout/&t={urllib.parse.quote(csrf_token, safe='')}"
        logger.debug("Constructed logout URL from CSRF: %s", logout_url)
        return logout_url
    logger.debug("Logout link not found in HTML")
    return None


# ─── Strip & Rewrite to HTML 2.0 ──────────────────────────────────────────────
def strip_to_html2(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

# ── START YO ADD SNIPLETS HERE AFTER ───────────────────────────────────────


    # ── Replace signature aside blocks with plain text inside [ ... ] ───────────
    for aside in soup.find_all("aside", class_="message-signature"):
        # Convert all proxy.php links to direct links
        for a in aside.find_all("a", href=True):
            href = a["href"]
            # If it's a proxy.php?link=... link, replace with direct link
            if "proxy.php?link=" in href:
                match = re.search(r'link=([^&]+)', href)
                if match:
                    real_url = urllib.parse.unquote(match.group(1))
                    a["href"] = real_url
                    a.string = real_url
            else:
                a.string = a["href"]
        # Get all text content (including from <br>)
        # We'll convert <br> to newlines, then collapse to a single string
        text = aside.get_text(separator="\n", strip=True)
        # Wrap the text in [ ... ] and <br> before and after
        replacement = f"<br><br>[<i>{text}</i>]<br>"
        logger.debug("Replaced <aside class='message-signature'>…</aside> with: %r", replacement)
        logger.debug(f"Unwrapping tags, allowed: {ALLOWED_TAGS}")
        aside.replace_with(BeautifulSoup(replacement, "html.parser"))

    for a in soup.find_all("a", role="button"):
        if a.get_text(strip=True).lower() == "click to expand...":
            a.decompose()
            logger.debug("Removed <a role='button'>Click to expand...</a>")


    # ── ADD SNIPPET: Flatten “Important Information” header ───────────────
    for h2 in soup.find_all('h2'):
        # look for the specific link inside the <h2>
        a = h2.find(
            'a',
            href=re.compile(r'^/bb/index\.php#important-information\.\d+')
        )
        if a:
            text = a.get_text(strip=True)
            # replace the entire <h2>…</h2> with plain text plus colon
            h2.replace_with(f"{text}:")
            logger.debug(
                "Flattened Important Information header to %r",
                f"{text}:"
            )


    # ── BOLD “threads” LINKS ────────────────────────────────────────────────
    for a in soup.find_all('a', href=re.compile(r'/bb/index\.php\?threads')):
        # Capture whatever is already inside the <a>
        inner_html = ''.join(str(c) for c in a.contents)
        a.clear()  # remove the old contents
        # Create <b> and put the old contents back inside it
        b = soup.new_tag('b')
        b.append(BeautifulSoup(inner_html, 'html.parser'))
        a.append(b)
        logger.debug("Bolded thread link: %s", a['href'])



    # ── CACHE JoyPixels emoji & rewrite to local /cached_image path (color GIF) ───
    import os
    from utils.image_utils import fetch_and_cache_image

    for img in soup.find_all("img", src=lambda v: v and "cdn.jsdelivr.net/joypixels" in v):
        original = img["src"]
        try:
            # download & cache returns the local filename (may include dirs)
            fname = fetch_and_cache_image(original)
            # only keep the basename to avoid double /cached_image/
            basename = os.path.basename(fname)
            # rewrite to our cached GIF (will be color)
            img["src"] = f"/cached_image/{basename}"
            # --- START ADDITION ---
            # Set explicit width and height for a normal emoji size
            img["width"] = "20"
            img["height"] = "20"
            # --- END ADDITION ---
            logger.debug("Cached JoyPixels emoji %s → /cached_image/%s (color GIF)", original, basename)
        except Exception as e:
            logger.warning("Failed to cache emoji %s: %r", original, e)

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

# old style... just rip of all ul.... but then you will loose them in posts too....

    # Remove all upstream <ul> menus
    # for ul in soup.find_all("ul"):
    #     ul.decompose()
    # logger.debug("Removed all upstream <ul> menus")

# new style.... try to preserve them in posts

    # ── REMOVE: any <ul> whose descendants have <a data-nav-id="…"> *unless* they also
    #    contain a <li data-xf-list-type="ul"> (i.e. content‐lists)
    for ul in soup.find_all("ul"):
        # only remove if it has at least one <a data-nav-id="…"> and no <li data-xf-list-type="ul">
        has_nav_id = bool(ul.find("a", attrs={"data-nav-id": True}))
        has_xf_list = bool(ul.find("li", attrs={"data-xf-list-type": "ul"}))
        if has_nav_id and not has_xf_list:
            ul.decompose()
            logger.debug("Removed upstream XenForo menu <ul> (had data-nav-id, no data-xf-list-type)")

# and remove this part:

# <ul itemscope="" itemtype="https://schema.org/BreadcrumbList">
# <li itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
# <a href="http://68kmla.org/bb/" itemprop="item">
# Home
# </a>
# ....etc

    # ── REMOVE: breadcrumb <ul> blocks with schema.org/BreadcrumbList ─────────
    for ul in soup.find_all("ul", attrs={"itemtype": "https://schema.org/BreadcrumbList"}):
        ul.decompose()
        logger.debug("Removed breadcrumb <ul> (schema.org/BreadcrumbList)")


# remove even more.............


    # ── REMOVE: conversations menu <ul> ───────────────────────────────────────
    for ul in soup.find_all("ul"):
        # if any <a> inside points to conversations (e.g. ?conversations/), delete the whole <ul>
        if ul.find("a", href=re.compile(r"^/bb/index\.php\?conversations/")):
            ul.decompose()
            logger.debug("Removed conversations <ul> (Show all / Start a new conversation)")


# remove Conversations when logged in

    # ── REMOVE logged-in “Conversations” block (with its <ul>) ─────────
    for h3 in soup.find_all("h3", string=lambda t: t and t.strip() == "Conversations"):
        # If there is a <ul> immediately after that <h3>, remove it too
        next_ul = h3.find_next_sibling("ul")
        if next_ul:
            next_ul.decompose()
        h3.decompose()
        logger.debug("Removed logged-in Conversations section")


    # ── REMOVE: Alerts anchor, heading, and related <ul> ──────────────────────
    #  Remove the <a aria-label="Alerts" …> element
    alerts_anchor = soup.find("a", attrs={"aria-label": "Alerts"})
    if alerts_anchor:
        alerts_anchor.decompose()
        logger.debug("Removed Alerts <a aria-label='Alerts'>")

    #  Remove the <h3>Alerts</h3> (if present)
    alerts_h3 = soup.find("h3", string="Alerts")
    if alerts_h3:
        #  Remove the <h3> itself
        alerts_h3.decompose()
        logger.debug("Removed <h3>Alerts</h3>")

        #  Also remove the next <ul> that contains “Show all / Mark read / Preferences”
        next_ul = alerts_h3.find_next_sibling("ul")
        if next_ul:
            next_ul.decompose()
            logger.debug("Removed <ul> under Alerts heading")



    # Remove login/register links
    for a in soup.find_all("a", href=re.compile(r"/bb/index\.php\?login/|/bb/index\.php\?register/")):
        a.decompose()
    logger.debug("Removed upstream login/register links")

    # Insert custom navigation header
    custom_nav = BeautifulSoup('''
        <!-- TEST MENU -->
        <a href="/bb/index.php">Home</a> |
        <a href="/bb/index.php?forums/">Forums</a> |
        <a href="/bb/index.php?forums/68kmla-wiki.13/">Wiki</a> |
        <a href="/bb/index.php?whats-new/">What’s new</a> |
        <a href="/bb/index.php?media/">Media</a> |
        <a href="/bb/index.php?resources/">Resources</a> |
        <a href="/bb/index.php?members/">Members</a> |
        <a href="/forums/archive/">Snitz Archive</a> |
        <a href="https://www.patreon.com/68kmla">Patreon</a> |
        <a href="/bb/index.php?login/">Log in</a> |
        <a href="/bb/index.php?register/">Register</a> |
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
                <option value="/bb/index.php?account/">Account</option>
                <option value="/bb/index.php?conversations/">Conversations</option>
                <option value="/bb/index.php?conversations/add">Start conversation</option>
                <option value="/bb/index.php?account/alerts">Alerts</option>
                <option value="/bb/index.php?account/preferences">Preferences</option>
                <option value="/bb/index.php?search/">Search</option>
                <option value="/forums/archive/">Snitz Archive</option>
                <option value="/bb/index.php?forums/68kmla-wiki.13/">Wiki</option>
            </select>
        </form>
        <hr>
        <!-- END TEST MENU -->
    ''', 'html.parser')

    body = soup.body or soup
    if body.contents:
        body.insert(0, custom_nav)
    else:
        body.append(custom_nav)
    logger.debug("Inserted custom navigation header")

    # Insert two <br> after Search or Log in buttons
    for btn2 in soup.find_all("button", {"type": "submit"}):
        txt = btn2.get_text(strip=True).lower()
        if txt in ("search", "log in", "login"):
            logger.debug("Inserting <br><br> after '%s' button", txt)
            btn2.insert_after(soup.new_tag("br"))
            btn2.insert_after(soup.new_tag("br"))

    # Replace any <button> whose text matches: login, log in, search, send, find, go
    for btn in list(soup.find_all("button")):
        text = btn.get_text(strip=True).lower()
        if text in ("login", "log in", "search", "send", "find", "go"):
            new_input = soup.new_tag("input", type="submit", value=text.capitalize())
            btn.insert_after(new_input)
            btn.decompose()
            logger.debug("Replaced <button>…</button> with <input type='submit' value='%s'>", text)

    # Remove XenForo “out of date browser” warning
    for warn in soup.find_all('div', class_=lambda c: c and 'js-browserWarning' in c):
        logger.debug("Removing browser-warning div")
        warn.decompose()

    # Replace “Loading…” text
    for txt in soup.find_all(string=lambda t: isinstance(t, str) and "Loading…" in t):
        logger.debug("Replacing 'Loading…' text")
        txt.replace_with(txt.replace("Loading…", ""))

    # Drop logo links (home buttons)
    for a in soup.find_all('a', href=re.compile(r'^https?://68kmla\.org/bb/?$')):
        if a.find('img', src=re.compile(r'/bb/data/assets/logo/')):
            logger.debug("Removing logo link")
            a.decompose()

    # Remove “Menu” button
    for btn in soup.find_all('button', attrs={"aria-label": "Menu"}):
        logger.debug("Removing Menu button")
        btn.decompose()

    # Rewrite attachment-preview links to direct image URL
    for a in soup.find_all('a', href=re.compile(r'^/bb/index\.php\?attachments/')):
        old = a['href']
        img = a.find('img')
        if img and img.get('src'):
            new = img['src']
            a['href'] = new
            logger.debug("Rewrote attachment link %s → %s", old, new)


# ── ADJUST <img> TAG DIMENSIONS FOR OLD BROWSERS ───────────────────────────

    # ── ADJUST <img> TAG DIMENSIONS FOR OLD BROWSERS ───────────────────────────
    from PIL import Image, UnidentifiedImageError
    import io

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src or src.startswith("data:"):
            continue
        clean_src = src.split('?', 1)[0]
        if clean_src.startswith('/'):
            full_src = f"https://{DOMAIN}{clean_src}"
        elif clean_src.startswith('http'):
            full_src = clean_src
        else:
            full_src = f"https://{DOMAIN}/{clean_src.lstrip('/')}"
        try:
            r = SESSION.get(full_src, headers={'User-Agent': request.headers.get('User-Agent','')})
            im = Image.open(io.BytesIO(r.content))
            w, h = im.size
            max_w, max_h = 512, 342
            scale = min(max_w / w, max_h / h, 1)
            new_w, new_h = int(w * scale), int(h * scale)
            img["width"], img["height"] = str(new_w), str(new_h)
            logger.debug("Scaled <%s> from %dx%d to %dx%d", src, w, h, new_w, new_h)
        except UnidentifiedImageError:
            logger.debug("Skipping resize for %r (unidentified image)", src)
        except Exception as e:
            logger.debug("Couldn't fetch or process image %r: %s", src, e)



# ────────────────────────────────────────────────────────────────────────────



    # ── ADD spacing around every <img> so old browsers break lines correctly
    for img in soup.find_all("img"):
        # insert a <br> immediately before and after each image
        img.insert_before(soup.new_tag("br"))
        img.insert_after(soup.new_tag("br"))
        logger.debug("Wrapped <img> in <br> tags for spacing: %s", img.get("src"))

    # Insert <hr> before every <h1>
    for h1 in soup.find_all("h1"):
        h1.insert_before(soup.new_tag("hr"))
        logger.debug("Inserted <hr> before <h1>")

    # Insert two <br> before every avatar link
    for a in soup.find_all('a'):
        img = a.find('img', src=re.compile(r'/bb/data/avatars/'))
        if img:
            a.insert_before(soup.new_tag("br"))
            a.insert_before(soup.new_tag("br"))
            logger.debug("Inserted <br><br> before avatar link to %s", a['href'])

    # Remove <script>, <style>, <link>, <noscript>, <svg>, and comments
    for t in soup.find_all(['script','style','link','noscript','svg']):
        t.decompose()
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        c.extract()

    # Remove XenForo client-load-time hidden form
    for form in soup.find_all("form", hidden=True):
        if form.find("input", {"id": "_xfClientLoadTime"}):
            form.decompose()
            logger.debug("Removed hidden _xfClientLoadTime form")

    # Image stripping or PNG→JPEG re-encode (tags remain; actual fetch in handle_request)
    if not ENABLE_IMAGES:
        for img in soup.find_all('img'):
            img.decompose()
    else:
        pass

    # Unwrap tags not valid in HTML 2.0
    for tag in list(soup.find_all()):
        if tag.name.lower() not in ALLOWED_TAGS:
            tag.unwrap()

    # Insert <hr><br> around XenForo credit link
    for credit in soup.find_all("a", href=re.compile(r"https?://xenforo\.com"), rel=lambda v: v and "sponsored" in v):
        credit.insert_before(soup.new_tag("br"))
        credit.insert_before(soup.new_tag("br"))
        credit.insert_before(soup.new_tag("hr"))
        credit.insert_after(soup.new_tag("br"))
        logger.debug("Inserted <hr><br><br> after XenForo credit link")

    # Remove “Top” scroll-to link
    for a in soup.find_all("a", attrs={"data-xf-click": "scroll-to"}, string="Top"):
        a.decompose()
        logger.debug("Removed 'Top' scroll-to link")

    # Remove “Install the app” block
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

    # Remove any standalone “Install the app” text nodes
    for txt in soup.find_all(string=lambda s: isinstance(s, NavigableString) and "Install the app" in s):
        logger.debug('Removing text node containing "Install the app"')
        txt.replace_with(txt.replace("Install the app", ""))

    # Strip unwanted attributes & set our own on <body>
    body = soup.find("body")
    if body and "data-template" in body.attrs:
        logger.debug("Removing data-template attribute from <body>")
        del body["data-template"]

    # --- START ADDITION: Remove <br> tags around cached emoji images ---
    # Find all <img> tags that are identified as cached emoji (src contains /cached_image/)
    for img in soup.find_all("img", src=lambda v: v and "/cached_image/" in v):
        # Check if the previous sibling is a <br> tag and remove it
        prev_sibling = img.previous_sibling
        if prev_sibling and prev_sibling.name == "br":
            prev_sibling.decompose()
            logger.debug("Removed <br> before cached emoji: %s", img.get("src"))
        
        # Check if the next sibling is a <br> tag and remove it
        next_sibling = img.next_sibling
        if next_sibling and next_sibling.name == "br":
            next_sibling.decompose()
            logger.debug("Removed <br> after cached emoji: %s", img.get("src"))
    # --- END ADDITION ---

    # after all your <img> tweaks etc., return the cleaned HTML
    return str(soup)

# ─── Remove empty lines helper ───────────────────────────────────────────────
def clean_empty_lines(s: str) -> str:
    return "\n".join(line for line in s.splitlines() if line.strip())


# ─── Wrap into minimal HTML 2.0 skeleton ────────────────────────────────────

def wrap_html2(inner: str, title: str, debug: str = "", user_id: str = None) -> str:
    # ─── Detect logout request and clear our session cache ────────────────
    global USERNAME, SESSION
    # if the incoming request URL was ...index.php?logout/&t=...
    #if request.path.endswith('index.php') and 'logout' in request.args:
    if request.full_path.startswith('/bb/index.php?logout'):
        logger.debug("Detected logout request → clearing SESSION.cookies and USERNAME")
        SESSION.cookies.clear()
        USERNAME = None

    dbg = f"<p style='color:red'>{debug}</p>" if ENABLE_DEBUG and debug else ""
    snippet = inner[:200].replace("\n", " ").replace("\r", " ")
    logger.debug("wrap_html2: inner snippet (first 200 chars): %r", snippet)
    if user_id:
        logger.debug("wrap_html2: Received user_id -> %r", user_id)
    else:
        logger.debug("wrap_html2: user_id was None")

    user = get_username()
    logout_url = extract_logout_link(inner)
    logger.debug("wrap_html2: logout_url -> %r", logout_url)

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
        <option value="/bb/index.php?account/security">Password and security</option>
        <option value="/bb/index.php?account/privacy">Privacy</option>
        <option value="/bb/index.php?account/preferences">Preferences</option>
        <option value="">------------</option>
        {f'<option value="{logout_url}">Log out</option>' if logout_url else ''}
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
        f"   <title>{title}</title>\n"
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

    # Follow redirect if necessary
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
        # Extract user_id from r2.text before stripping
        orig_soup = BeautifulSoup(r2.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        if span:
            user_id = span["data-user-id"]
        else:
            user_id = None

        inner = strip_to_html2(r2.text)
        title = (BeautifulSoup(r2.text, 'html.parser').title or f"Search: {q}").string
        return wrap_html2(inner, title, debug, user_id), 200

    # Otherwise
    orig_soup = BeautifulSoup(r1.text, "html.parser")
    span = orig_soup.find("span", attrs={"data-user-id": True})
    if span:
        user_id = span["data-user-id"]
    else:
        user_id = None

    inner = strip_to_html2(r1.text)
    title = (BeautifulSoup(r1.text, 'html.parser').title or f"Search: {q}").string
    return wrap_html2(inner, title, debug, user_id), r1.status_code


# ─── Login Flow ─────────────────────────────────────────────────────────────
def _do_login(req, debug: str):
    url0 = f"https://{DOMAIN}/bb/index.php?login/"
    r0   = SESSION.get(url0, headers={'User-Agent': req.headers.get('User-Agent','')})
    if ENABLE_DEBUG:
        debug += (
            "<b>68kMLA Response:</b><br>"
            f"Status: {r0.status_code}<br>Headers: {dict(r0.headers)}<br><br>"
        )

    s0   = BeautifulSoup(r0.text, 'html.parser')
    xf   = s0.find('input', {'name': '_xfToken'})
    data = dict(req.form)
    if xf:
        data['_xfToken'] = xf['value']

    r1 = SESSION.post(
        f"https://{DOMAIN}/bb/index.php?login/login",
        headers={'User-Agent': req.headers.get('User-Agent',''), 'Referer': url0},
        data=data,
        allow_redirects=False
    )
    if ENABLE_DEBUG:
        debug += (
            "<b>68kMLA Response:</b><br>"
            f"Status: {r1.status_code}<br>Headers: {dict(r1.headers)}<br><br>"
        )

    # If redirect (successful login), follow
    if r1.status_code in (301, 302, 303):
        loc = r1.headers.get('Location', '')
        if loc.startswith('/'):
            loc = f"https://{DOMAIN}{loc}"
        r2 = SESSION.get(loc, headers={'User-Agent': request.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r2.status_code}<br>Headers: {dict(r2.headers)}<br><br>"
            )
        # Extract user_id from the logged-in page
        orig_soup = BeautifulSoup(r2.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        if span:
            user_id = span["data-user-id"]
        else:
            user_id = None

        inner = strip_to_html2(r2.text)
        title = (BeautifulSoup(r2.text, 'html.parser').title or "Logged In").string
        return wrap_html2(inner, title, debug, user_id), 200

    # If login failed (no redirect), show whatever r1 returned
    orig_soup = BeautifulSoup(r1.text, "html.parser")
    span = orig_soup.find("span", attrs={"data-user-id": True})
    if span:
        user_id = span["data-user-id"]
    else:
        user_id = None

    inner = strip_to_html2(r1.text)
    title = (BeautifulSoup(r1.text, 'html.parser').title or "Login Result").string
    return wrap_html2(inner, title, debug, user_id), 200


# ─── Main Entry Point ────────────────────────────────────────────────────────
def handle_request(req):
    full = req.full_path            # e.g. "/bb/proxy.php?image=…"
    path = req.path.lstrip('/')     # e.g. "bb/proxy.php"
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

    # ── BYPASS + CONVERT XenForo proxy.php IMAGE URLs ────────────────────────
    # If this is a GET to “proxy.php?image=…”, treat it as a raw image:
    if req.method == 'GET' and path.endswith('proxy.php') and 'image=' in qs:
        url = f"https://{DOMAIN}/{path}?{qs}"
        logger.debug("Fetching XenForo proxy.php image: %s", url)
        r = SESSION.get(url)
        orig_ct = r.headers.get('Content-Type', '').lower()
        logger.debug("Upstream proxy.php returned %d bytes @ %s", len(r.content), orig_ct)

        img_bytes = r.content
        out_ct    = orig_ct

        # Always try to re-encode (flatten PNG→JPEG, etc.)
        if r.status_code == 200 and orig_ct.startswith('image/'):
            try:
                img = Image.open(io.BytesIO(r.content))
                logger.debug("PIL opened proxied image: format=%s mode=%s size=%s",
                             img.format, img.mode, img.size)

                # If it has transparency, paste onto white
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                    logger.debug("Proxied image has transparency; flattening onto white")
                    bg = Image.new('RGB', img.size, (255, 255, 255))
                    rgba = img.convert('RGBA')
                    bg.paste(rgba, mask=rgba.split()[-1])
                    img = bg
                else:
                    img = img.convert('RGB')

                buf = io.BytesIO()
                img.save(buf, 'JPEG', progressive=False)
                img_bytes = buf.getvalue()
                out_ct    = 'image/jpeg'
                logger.debug("Re‐encoded proxied image to JPEG, size=%d", len(img_bytes))
            except Exception as e:
                logger.debug("PIL failed for proxied image; returning raw bytes: %r", e)

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

    # …rest of your existing handle_request(…) code continues here…

# --- end enter block ----



    # ─── 1) attachments → binary + PIL re-encode (with resize/convert)
    if req.method == 'GET' and 'attachments/' in full:
        url = f"https://{DOMAIN}{full}"
        logger.debug("Fetching attachment: %s", url)
        r = SESSION.get(url)
        orig_ct = r.headers.get('Content-Type','').lower()
        logger.debug("Attachment response: %d bytes @ %s", len(r.content), orig_ct)

        img_bytes = r.content
        out_ct    = orig_ct

        # only try to re-encode real images
        if r.status_code == 200 and orig_ct.startswith('image/'):
            try:
                img = Image.open(io.BytesIO(r.content))
                logger.debug("PIL opened attachment: format=%s mode=%s size=%s",
                             img.format, img.mode, img.size)

                # (A) flatten alpha onto white if needed
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                    logger.debug("Attachment has transparency; flattening onto white")
                    bg = Image.new('RGB', img.size, (255, 255, 255))
                    rgba = img.convert('RGBA')
                    bg.paste(rgba, mask=rgba.split()[-1])
                    img = bg
                else:
                    img = img.convert('RGB')

                # (B) resize if requested by config
                if getattr(config, "RESIZE_IMAGES", False):
                    orig_w, orig_h = img.size
                    max_w = getattr(config, "MAX_IMAGE_WIDTH", orig_w)
                    max_h = getattr(config, "MAX_IMAGE_HEIGHT", orig_h)
                    scale = min(max_w / orig_w, max_h / orig_h, 1.0)
                    if scale < 1.0:
                        new_size = (int(orig_w * scale), int(orig_h * scale))
                        img = img.resize(new_size, Image.LANCZOS)
                        logger.debug("Resized attachment to %s", new_size)

                # (C) convert to GIF if requested, else JPEG
                if getattr(config, "CONVERT_IMAGES", False) and \
                   getattr(config, "CONVERT_IMAGES_TO_FILETYPE", "").lower() == "gif":
                    buf = io.BytesIO()
                    algo_name = getattr(config, "DITHERING_ALGORITHM", "FLOYDSTEINBERG").upper()
                    dither_const = getattr(Image, algo_name, Image.FLOYDSTEINBERG)
                    img.convert('P', dither=dither_const).save(buf, 'GIF')
                    img_bytes = buf.getvalue()
                    out_ct    = 'image/gif'
                    logger.debug("Converted attachment to GIF, size=%d", len(img_bytes))
                else:
                    buf = io.BytesIO()
                    img.save(buf, 'JPEG', progressive=False)
                    img_bytes = buf.getvalue()
                    out_ct    = 'image/jpeg'
                    logger.debug("Re-encoded attachment to JPEG, size=%d", len(img_bytes))

            except Exception as e:
                logger.debug("PIL failed for attachment; sending raw: %r", e)
                img_bytes = r.content
                out_ct    = orig_ct

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

        img_bytes = r.content
        out_ct    = orig_ct

        try:
            buf_in = io.BytesIO(r.content)
            img = Image.open(buf_in)
            logger.debug("PIL opened image: format=%s mode=%s size=%s", img.format, img.mode, img.size)

            # (A) Flatten alpha onto white if needed
            if img.mode in ('RGBA','LA') or (img.mode == 'P' and 'transparency' in img.info):
                bg = Image.new('RGB', img.size, (255,255,255))
                rgba = img.convert('RGBA')
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            else:
                img = img.convert('RGB')

            # (B) Resize if requested by config
            if getattr(config, "RESIZE_IMAGES", False):
                orig_w, orig_h = img.size
                max_w = getattr(config, "MAX_IMAGE_WIDTH", orig_w)
                max_h = getattr(config, "MAX_IMAGE_HEIGHT", orig_h)
                scale = min(max_w / orig_w, max_h / orig_h, 1.0)
                if scale < 1.0:
                    new_size = (int(orig_w * scale), int(orig_h * scale))
                    img = img.resize(new_size, Image.LANCZOS)
                    logger.debug("Resized image to %s", new_size)

            # (C) Convert to GIF if requested, else JPEG
            if getattr(config, "CONVERT_IMAGES", False) and \
               getattr(config, "CONVERT_IMAGES_TO_FILETYPE", "").lower() == "gif":
                buf_out = io.BytesIO()
                algo_name = getattr(config, "DITHERING_ALGORITHM", "FLOYDSTEINBERG").upper()
                dither_const = getattr(Image, algo_name, Image.FLOYDSTEINBERG)
                img.convert('P', dither=dither_const).save(buf_out, 'GIF')
                img_bytes = buf_out.getvalue()
                out_ct    = 'image/gif'
                logger.debug("Converted image to GIF, size=%d", len(img_bytes))
            else:
                buf_out = io.BytesIO()
                img.save(buf_out, 'JPEG', progressive=False)
                img_bytes = buf_out.getvalue()
                out_ct    = 'image/jpeg'
                logger.debug("Re-encoded image to JPEG, size=%d", len(img_bytes))

        except Exception as e:
            logger.debug("PIL failed (%r), falling back to raw bytes", e)
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






    # ─── 3) Direct Snitz Archive (static HTML + images under /forums/archive/) ─
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

    # ─── 7) GET login form ────────────────────────────────────────────────────
    if req.method == 'GET' and 'login/' in full and 'login/login' not in full:
        url = f"https://{DOMAIN}/bb/index.php?login/"
        logger.debug("Fetching login form: %s", url)
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
        title = (BeautifulSoup(r.text, 'html.parser').title or 'Login').string
        return wrap_html2(inner, title, debug, user_id), 200






    # ─── GET register form ───────────────────────────────────────────────────  
    if req.method == 'GET' and 'register/' in full and 'register/register' not in full:
        url = f"https://{DOMAIN}/bb/index.php?register/"
        logger.debug("Fetching register form: %s", url)
        r = SESSION.get(url, headers={'User-Agent': request.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r.status_code}<br>Headers: {dict(r.headers)}<br><br>"
            )
        orig_soup = BeautifulSoup(r.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        user_id = span["data-user-id"] if span else None

        inner = strip_to_html2(r.text)
        title = (orig_soup.title or BeautifulSoup(r.text, 'html.parser').title or "Register").string
        return wrap_html2(inner, title, debug, user_id), 200

    # ─── POST register ───────────────────────────────────────────────────────  
    if req.method == 'POST' and 'register/register' in full:
        url = f"https://{DOMAIN}/{path}"
        logger.debug("Submitting registration: %s", url)
        r = SESSION.post(
            url,
            data=req.form,
            headers={'User-Agent': request.headers.get('User-Agent','')},
            allow_redirects=False
        )

        # on success there’s usually a redirect
        if r.status_code in (301, 302, 303):
            loc = r.headers.get('Location', '')
            if loc.startswith('/'):
                loc = f"https://{DOMAIN}{loc}"
            logger.debug("Registration redirected to %s", loc)
            r2 = SESSION.get(loc, headers={'User-Agent': request.headers.get('User-Agent','')})
            orig = BeautifulSoup(r2.text, "html.parser")
            span = orig.find("span", attrs={"data-user-id": True})
            user_id = span["data-user-id"] if span else None

            inner = strip_to_html2(r2.text)
            title = (orig.title or BeautifulSoup(r2.text, 'html.parser').title or "Registration Complete").string
            return wrap_html2(inner, title, debug, user_id), 200

        # otherwise show any errors (e.g. validation failures)
        orig = BeautifulSoup(r.text, "html.parser")
        span = orig.find("span", attrs={"data-user-id": True})
        user_id = span["data-user-id"] if span else None

        inner = strip_to_html2(r.text)
        title = (orig.title or BeautifulSoup(r.text, 'html.parser').title or "Registration Result").string
        return wrap_html2(inner, title, debug, user_id), r.status_code



    # ─── 8) POST login ─────────────────────────────────────────────────────────
    if req.method == 'POST' and 'login/login' in full:
        return _do_login(req, debug)

    # ─── 9) Home page ─────────────────────────────────────────────────────────
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

    # ─── 10) All other index.php pages ────────────────────────────────────────
    if req.method == 'GET' and 'index.php' in path:
        url = f"https://{DOMAIN}/{path}" + (f'?{qs}' if qs else '')
        logger.debug("Fetching other page: %s", url)
        try:
            r = SESSION.get(
                url,
                headers={'User-Agent': request.headers.get('User-Agent','')}
            )
        except requests.exceptions.RequestException as e:
            logger.warning("Upstream fetch failed for %s: %s", url, e)
            error_html = (
                "<h1>Page Unavailable</h1>"
                "<p>Sorry, that page is temporarily unreachable.</p>"
                "Read about the wiki in the Wiki forum:<br>"
                "<a href=\"https://68kmla.org/bb/index.php?forums/68kmla-wiki.13/\">WIKI forum</a>"
            )
            return wrap_html2(error_html, "Unavailable", debug, None), 503

        # ─── Success path ───────────────────────────────────────────────────
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r.status_code}<br>Headers: {dict(r.headers)}<br><br>"
            )

        orig_soup = BeautifulSoup(r.text, "html.parser")
        span = orig_soup.find("span", attrs={"data-user-id": True})
        user_id = span["data-user-id"] if span else None

        inner = strip_to_html2(r.text)
        title = (orig_soup.title or BeautifulSoup(r.text, 'html.parser').title or '68kMLA').string
        return wrap_html2(inner, title, debug, user_id), 200



    # ─── 11) Handle add-reply POSTs ───────────────────────────────────────────
    if req.method == 'POST' and 'add-reply' in full:
        url = f"https://{DOMAIN}{full}"
        logger.debug("Proxying add-reply POST to %s", url)
        r = SESSION.post(
            url,
            data=req.form,
            headers={'User-Agent': request.headers.get('User-Agent','')},
            allow_redirects=False
        )

        # Follow redirects back into forum
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

        # Otherwise render POST result
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

    # ─── Method Not Allowed (if we fall through) ──────────────────────────────
    logger.debug("Method not allowed: %s %s", req.method, req.full_path)
    inner = (
        "<h1>405 – Method Not Allowed</h1>"
        "<p>Sorry, this proxy can’t handle that request.</p>"
        "<p><a href=\"/bb/index.php\">Return to 68kMLA Home</a></p>"
    )
    return wrap_html2(inner, "Error – Method Not Allowed", debug, None), 405
