from flask import request
from bs4 import BeautifulSoup, Comment, NavigableString
import requests, urllib.parse, re, logging
from PIL import Image
import io

# --- CONFIG ----------------------------------------------------------------

ENABLE_DEBUG  = False     # True -> show debug banners + DEBUG logs
ENABLE_IMAGES = True     # True -> allow <img>; False -> strip all images

# --- END CONFIG ------------------------------------------------------------

SESSION = requests.Session()
DOMAIN  = "68kmla.org"
USERNAME = None  # cache

# HTML 2.0-legal tags
ALLOWED_TAGS = {
    'html','head','title',
    'body','center',
    'h1','h2','h3','h4','h5','h6',
    'p','ul','li','a','br','hr',
    'form','input','textarea','select','option','button',
    'img','b'
}

# --- journalctl handler import ----------------------------------------------
try:
    from systemd.journal import JournalHandler as JournaldLogHandler
except ImportError:
    JournaldLogHandler = None

# --- set up logging ---------------------------------------------------------
logger = logging.getLogger("68kmlaorg")
logger.setLevel(logging.DEBUG if ENABLE_DEBUG else logging.INFO)

# journald
if JournaldLogHandler:
    jh = JournaldLogHandler()
    jh.setLevel(logging.DEBUG)
    jh.setFormatter(logging.Formatter('[68kMLA] %(levelname)s: %(message)s'))
    logger.addHandler(jh)

# fallback stderr
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG if ENABLE_DEBUG else logging.INFO)
ch.setFormatter(logging.Formatter('[68kMLA] %(levelname)s: %(message)s'))
logger.addHandler(ch)

# --- helper: get logged-in username -----------------------------------------
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

# --- strip & rewrite to HTML 2.0 --------------------------------------------
def strip_to_html2(html):
    soup = BeautifulSoup(html, "html.parser")

    # --- remove all existing <hr> tags before applying modifications ------------
    for hr in soup.find_all("hr"):
        hr.decompose()
    logger.debug("Removed all existing <hr> tags from upstream HTML")

    #  --- remove any inline base64 images (data: URIs) --------------------
    for img in soup.find_all("img", src=lambda v: v and v.startswith("data:")):
        img.decompose()


    # remove any inline data: URLs inside style attributes
    for tag in soup.find_all(attrs={"style": True}):
        if "data:image" in tag["style"]:
            logger.debug("Removed style with inline image")
            del tag["style"]

    # --- add two <br> after any `<button type="submit">Search</button>` or `<button type="submit">Log in</button>`
    for btn2 in soup.find_all("button", {"type": "submit"}):
        txt = btn2.get_text(strip=True).lower()
        if txt == "search":
            logger.debug("Inserting <br><br> after Search button")
            btn2.insert_after(soup.new_tag("br"))
            btn2.insert_after(soup.new_tag("br"))
        elif txt in ("log in", "login"):
            logger.debug("Inserting <br><br> after Log in button")
            btn2.insert_after(soup.new_tag("br"))
            btn2.insert_after(soup.new_tag("br"))
    
    # 1) change <button type="submit">....</button>
    # --- f. i. into <input type="submit" value="Log in"> ------------------------
    for btn in soup.find_all("button", {"type": "submit"}):
        text = btn.get_text(strip=True)
        # handle both "Log in" and "Login"
        if text.lower() in ("login", "log in", "search", "send"):
            new_input = soup.new_tag("input", type="submit", value=text)
            btn.replace_with(new_input)
            logger.debug(
                "Replaced <button>...</button> with <input type='submit' value='%s'>",
                text
            )

    # --- handle Go-buttons. Will give a submit button, but the form is not working properly, so commented 
    #for btn in soup.find_all("button", {"type": "button"}):
    #    if btn.get_text(strip=True).lower() == "go":
    #        new_input = soup.new_tag("input", type="submit", value="Go")
    #        btn.replace_with(new_input)
    #        logger.debug("Replaced <button type='button'>Go</button> with <input type='submit' value='Go'>")

    # 2) remove the XenForo "out of date browser" warning
    for warn in soup.find_all('div', class_=lambda c: c and 'js-browserWarning' in c):
        logger.debug("Removing browser-warning div")
        warn.decompose()

    # 3) replace any "Loading..." text with "Click here"
    for txt in soup.find_all(string=lambda t: isinstance(t, str) and "Loading…" in t):
        logger.debug("Replacing Loading… node")
        txt.replace_with(txt.replace("Loading…", ""))

    # 4) drop the two logo links (home buttons)
    for a in soup.find_all('a', href=re.compile(r'^https?://68kmla\.org/bb/?$')):
        if a.find('img', src=re.compile(r'/bb/data/assets/logo/')):
            logger.debug("Removing logo link")
            a.decompose()

    # 5) remove the "Menu" button
    for btn in soup.find_all('button', attrs={"aria-label":"Menu"}):
        logger.debug("Removing Menu button")
        btn.decompose()

    # 5a) rewrite attachment-preview links to direct image URL -------------
    for a in soup.find_all('a', href=re.compile(r'^/bb/index\.php\?attachments/')):
        old = a['href']
        img = a.find('img')
        if img and img.get('src'):
            new = img['src']
            a['href'] = new
            logger.debug("Rewrote attachment link %s -> %s", old, new)


    # --- INSERTION: before every <h1>, drop End 68kMLA navigation menu  ---------
    # for h1 in soup.find_all("h1"):
    #    comment = Comment(" test ")
    #    h1.insert_before(comment)
    #    h1.insert_before(BeautifulSoup("||||||||||||||||||||||||||||||  <hr>", "html.parser"))
    #
    #    logger.debug("Inserted <hr> ..... end 68kMLA navigation menu before <h1>")

    for h1 in soup.find_all("h1"):
        h1.insert_before(soup.new_tag("hr"))
        logger.debug("Inserted <hr> before <h1>")

    # 5b) insert two <br> before every avatar link ------------------------
    for a in soup.find_all('a'):
        img = a.find('img', src=re.compile(r'/bb/data/avatars/'))
        if img:
            a.insert_before(soup.new_tag("br"))
            a.insert_before(soup.new_tag("br"))
            logger.debug("Inserted <br><br> before avatar link to %s", a['href'])

    # --- INSERTION: add a <br> after the "Advanced search..." link -----------------
    for a in soup.find_all("a", href=re.compile(r'^/bb/index\.php\?search/?$')):
        # this matches <a href="/bb/index.php?search/">Advanced search...</a>
        a.insert_after(soup.new_tag("br"))
        logger.debug("Inserted <br> after Advanced search link")


    # 6) strip scripts/styles/comments/etc
    for t in soup.find_all(['script','style','link','noscript','svg']):
        t.decompose()
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        c.extract()


    # ── REMOVE: XenForo client-load-time hidden form ─────────────────────────
    for form in soup.find_all("form", hidden=True):
        if form.find("input", {"id": "_xfClientLoadTime"}):
            form.decompose()
            logger.debug("Removed hidden _xfClientLoadTime form")



    # 7) image stripping or PNG->JPEG re-encode
    if not ENABLE_IMAGES:
        for img in soup.find_all('img'):
            img.decompose()
    else:
        # leave the tags in place; image requests are handled separately
        pass

    # 8) unwrap any tags not valid in HTML 2.0
    for tag in list(soup.find_all()):
        if tag.name.lower() not in ALLOWED_TAGS:
            tag.unwrap()

    # ── WRAP: add <hr><br> around the XenForo credit link ────────────────────────
    for credit in soup.find_all("a", href=re.compile(r"https?://xenforo\.com"), rel=lambda v: v and "sponsored" in v):
        credit.insert_before(soup.new_tag("br"))
        credit.insert_before(soup.new_tag("br"))
        credit.insert_before(soup.new_tag("hr"))
        credit.insert_after(soup.new_tag("br"))
    #   credit.insert_after(soup.new_tag("br"))
        logger.debug("Inserted <hr><br><br> after XenForo credit link")
    
    # --- remove  <a data-xf-click="scroll-to" href="#top">Top</a>
    for a in soup.find_all("a", attrs={"data-xf-click": "scroll-to"}, string="Top"):
        a.decompose()
        logger.debug("Removed 'Top' scroll-to link")

    # -- REMOVE: the "Install the app" block -----------------------------------
    for install_button in soup.find_all("button", {"type": "button"}):
        if install_button.get_text(strip=True) == "Install":
            # drop the preceding <hr>
            prev = install_button.find_previous_sibling()
            if prev and prev.name == "hr":
                prev.decompose()

            # drop the standalone text node "Install the app"
            txt = install_button.previous_sibling
            if isinstance(txt, NavigableString) and "Install the app" in txt:
                txt.extract()

            # drop the button itself
            install_button.decompose()
            logger.debug("Removed Install-the-app block")


    # --- remove any standalone "Install the app" text nodes ----------------
    for txt in soup.find_all(string=lambda s: isinstance(s, NavigableString) and "Install the app" in s):
        logger.debug('Removing text node containing "Install the app"')
        txt.replace_with(txt.replace("Install the app", ""))

    return str(soup)

# --- remove empty lines -----------------------------------------------------
def clean_empty_lines(s):
    return "\n".join(line for line in s.splitlines() if line.strip())

# --- wrap into a minimal HTML 2.0 skeleton ---------------------------------
def wrap_html2(inner, title, debug=""):
    dbg = f"<p style='color:red'>{debug}</p>" if ENABLE_DEBUG and debug else ""
    user = get_username()
    lg  = f"<p>Logged in as {user}</p>" if user else ""
    nav = (
        "\n"
        "<hr>\n"
        "\n"
    )
    ftr = (
        "<hr>\n"
        "<p>&copy; 2025 68kMLA -- "
        '<a href="#top">Top</a> | '
        '<a href="/bb/index.php?help/">Help</a> | '
        '<a href="/bb/index.php?misc/contact">Contact</a>'
        "</p>\n"
    )
    html = (
        "<html><head>\n"
          f"  <title>{title}</title>\n"
        "</head><body>\n"
          f"{dbg}{lg}{nav}"
          f"{inner}\n"
          f"{ftr}"
        "</body></html>\n"
    )
    return clean_empty_lines(html)

# --- perform search flow ----------------------------------------------------
def _do_search(q, req, debug):
    form_url = f"https://{DOMAIN}/bb/index.php?search/"
    logger.debug("Search form URL: %s", form_url)
    r0 = SESSION.get(form_url, headers={'User-Agent': req.headers.get('User-Agent','')})
    if ENABLE_DEBUG:
        debug += (
            "<b>68kMLA Response:</b><br>"
            f"Status: {r0.status_code}<br>Headers: {dict(r0.headers)}<br><br>"
        )

    s0 = BeautifulSoup(r0.text,'html.parser')
    xf = s0.find('input',{'name':'_xfToken'})
    data = {'keywords': q}
    if xf: data['_xfToken'] = xf['value']

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

    # follow redirect
    if r1.status_code in (301,302,303):
        loc = r1.headers.get('Location','')
        if loc.startswith('/'):
            loc = f"https://{DOMAIN}{loc}"
        logger.debug("Redirecting search to %s", loc)
        r2 = SESSION.get(loc, headers={'User-Agent': req.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r2.status_code}<br>Headers: {dict(r2.headers)}<br><br>"
            )
        inner = strip_to_html2(r2.text)
        title = (BeautifulSoup(r2.text,'html.parser').title or f"Search: {q}").string
        return wrap_html2(inner, title, debug), 200

    inner = strip_to_html2(r1.text)
    title = (BeautifulSoup(r1.text,'html.parser').title or f"Search: {q}").string
    return wrap_html2(inner, title, debug), r1.status_code

# --- perform login flow -----------------------------------------------------
def _do_login(req, debug):
    url0 = f"https://{DOMAIN}/bb/index.php?login/"
    r0   = SESSION.get(url0, headers={'User-Agent': req.headers.get('User-Agent','')})
    if ENABLE_DEBUG:
        debug += (
            "<b>68kMLA Response:</b><br>"
            f"Status: {r0.status_code}<br>Headers: {dict(r0.headers)}<br><br>"
        )

    s0   = BeautifulSoup(r0.text,'html.parser')
    xf   = s0.find('input',{'name':'_xfToken'})
    data = dict(req.form)
    if xf: data['_xfToken'] = xf['value']

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

    if r1.status_code in (301,302,303):
        loc = r1.headers.get('Location','')
        if loc.startswith('/'):
            loc = f"https://{DOMAIN}{loc}"
        r2 = SESSION.get(loc, headers={'User-Agent': request.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r2.status_code}<br>Headers: {dict(r2.headers)}<br><br>"
            )
        inner = strip_to_html2(r2.text)
        title = (BeautifulSoup(r2.text,'html.parser').title or "Logged In").string
        return wrap_html2(inner, title, debug), 200

    inner = strip_to_html2(r1.text)
    title = (BeautifulSoup(r1.text,'html.parser').title or "Login Result").string
    return wrap_html2(inner, title, debug), 200

# --- main entrypoint --------------------------------------------------------
def handle_request(req):
    full = req.full_path
    path = req.path.lstrip('/')
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

    # 1) attachments -> binary + PIL re-encode (preserves colour, flattens transparency)
    from flask import Response

    if req.method=='GET' and 'attachments/' in full:
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

                # flatten alpha onto white
                if img.mode in ('RGBA','LA') or (img.mode=='P' and 'transparency' in img.info):
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

    # 1b) avatars & other /data/... images -> force-200 JPEG, no 304s
    from flask import Response
    if req.method == 'GET' and ('/data/avatars/' in full or '/data/assets/' in full):
        url = f"https://{DOMAIN}{full}"
        logger.debug("Fetching image URL: %s", url)
        r = SESSION.get(url)
        orig_ct = r.headers.get('Content-Type', '')
        logger.debug("Upstream returned %d bytes @ %s", len(r.content), orig_ct)

        img_bytes = r.content
        out_ct    = orig_ct

        # try PIL re-encode with alpha->white composite
        try:
            img = Image.open(io.BytesIO(r.content))
            logger.debug("PIL opened image: fmt=%s mode=%s size=%s",
                         img.format, img.mode, img.size)
            if img.mode in ('RGBA','LA') or (img.mode=='P' and 'transparency' in img.info):
                bg = Image.new('RGB', img.size, (255,255,255))
                alpha = img.convert('RGBA').split()[-1]
                bg.paste(img.convert('RGBA'), mask=alpha)
                img = bg
            else:
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, 'JPEG', progressive=False)
            img_bytes = buf.getvalue()
            out_ct    = 'image/jpeg'
            logger.debug("Re-encoded to JPEG, new size=%d", len(img_bytes))
        except Exception as e:
            logger.debug("PIL re-encode failed, using raw bytes: %r", e)

        # return a real 200-OK Flask Response & strip caching so no more 304s
        headers = {
            'Content-Type':   out_ct,
            'Cache-Control':  'no-cache, no-store, must-revalidate',
            'Pragma':         'no-cache',
            'Expires':        '0'
        }
        return Response(
            img_bytes,
            status=200,
            headers=headers,
            direct_passthrough=True
        )

    # 2) direct search-ID GET
    if req.method=='GET' and path.endswith('index.php') and 'search/' in qs:
        url = f"https://{DOMAIN}/{path}?{qs}"
        logger.debug("Direct search GET: %s", url)
        r = SESSION.get(url, headers={'User-Agent': req.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r.status_code}<br>Headers: {dict(r.headers)}<br><br>"
            )
        inner = strip_to_html2(r.text)
        title = (BeautifulSoup(r.text,'html.parser').title or 'Search').string
        return wrap_html2(inner, title, debug), 200

    # 3) quick-search POST
    if req.method=='POST' and 'search/search' in full:
        q = req.form.get('keywords','').strip()
        if not q:
            return wrap_html2("<p>No search term</p>","Error",debug),400
        return _do_search(q, req, debug)

    # 4) GET /search?q=
    if req.method=='GET' and path.rstrip('/')=='search' and 'q' in req.args:
        q = req.args.get('q','').strip()
        if not q:
            return wrap_html2("<p>No search term</p>","Error",debug),400
        return _do_search(q, req, debug)

    # 5) GET login form
    if req.method=='GET' and 'login/' in full and 'login/login' not in full:
        url = f"https://{DOMAIN}/bb/index.php?login/"
        logger.debug("Fetching login form: %s", url)
        r = SESSION.get(url, headers={'User-Agent': req.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r.status_code}<br>Headers: {dict(r.headers)}<br><br>"
            )
        inner = strip_to_html2(r.text)
        title = (BeautifulSoup(r.text,'html.parser').title or 'Login').string
        return wrap_html2(inner, title, debug), 200

    # 6) POST login
    if req.method=='POST' and 'login/login' in full:
        return _do_login(req, debug)

    # 7) home page
    if (req.method=='GET'
        and (not path or path in ['bb','bb/','bb/index.php'])
        and (not qs or qs.startswith('images='))):
        url = f"https://{DOMAIN}/bb/index.php"
        logger.debug("Fetching home page: %s", url)
        r = SESSION.get(url, headers={'User-Agent': req.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r.status_code}<br>Headers: {dict(r.headers)}<br><br>"
            )
        inner = strip_to_html2(r.text)
        title = (BeautifulSoup(r.text,'html.parser').title or '68kMLA Home').string
        return wrap_html2(inner, title, debug), 200

    # 8) all other index.php pages
    if req.method=='GET' and 'index.php' in path:
        url = f"https://{DOMAIN}/{path}" + (f'?{qs}' if qs else '')
        logger.debug("Fetching other page: %s", url)
        r = SESSION.get(url, headers={'User-Agent': req.headers.get('User-Agent','')})
        if ENABLE_DEBUG:
            debug += (
                "<b>68kMLA Response:</b><br>"
                f"Status: {r.status_code}<br>Headers: {dict(r.headers)}<br><br>"
            )
        inner = strip_to_html2(r.text)
        title = (BeautifulSoup(r.text,'html.parser').title or '68kMLA').string
        return wrap_html2(inner, title, debug), 200

    # ─── handle add-reply POSTs ───────────────────────────────────────────────
    if req.method == 'POST' and 'add-reply' in full:
        url = f"https://{DOMAIN}{full}"
        logger.debug("Proxying add-reply POST to %s", url)
        r = SESSION.post(
            url,
            data=req.form,
            headers={'User-Agent': request.headers.get('User-Agent','')},
            allow_redirects=False
        )

        # follow redirects back into the forum
        if r.status_code in (301, 302, 303):
            loc = r.headers.get('Location', '')
            if loc.startswith('/'):
                loc = f"https://{DOMAIN}{loc}"
            logger.debug("add-reply redirected to %s", loc)
            r2 = SESSION.get(loc, headers={'User-Agent': request.headers.get('User-Agent','')})
            inner = strip_to_html2(r2.text)
            title_tag = BeautifulSoup(r2.text, 'html.parser').title
            title = title_tag.string if title_tag else "Reply Posted"
            return wrap_html2(inner, title, debug), 200

        # otherwise render whatever the POST returned
        inner = strip_to_html2(r.text)
        title_tag = BeautifulSoup(r.text, 'html.parser').title
        title = title_tag.string if title_tag else "Reply Result"
        return wrap_html2(inner, title, debug), r.status_code


    # ─── nicer 405 “Method Not Allowed” page ─────────────────────────────────
    logger.debug("Method not allowed: %s %s", req.method, req.full_path)
    inner = (
        "<h1>405 – Method Not Allowed</h1>"
        "<p>Sorry, this proxy can’t handle that request.</p>"
        "<p><a href=\"/bb/index.php\">Return to 68kMLA Home</a></p>"
    )
    # wrap_html2 will automatically add your debug banner, login status, nav, footer, etc.
    return wrap_html2(inner, "Error – Method Not Allowed", debug), 405

