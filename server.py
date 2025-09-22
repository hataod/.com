# server.py — ХАТА© API / Одеса
import os, json, time, random, threading, sys, re, shutil, base64, logging
from datetime import datetime, timedelta, timezone
from flask import Flask, request, send_from_directory, jsonify, Response
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

# -------------------- Базовые настройки --------------------
PORT = 8000
DATA_FILE = 'data.json'

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
UPLOAD_DIR = os.path.join(STATIC_DIR, 'uploads')
BANNER_DIR = os.path.join(STATIC_DIR, 'banners')
HOT_DIR    = os.path.join(STATIC_DIR, 'hot')
ORDERS_DIR = os.path.join(STATIC_DIR, 'orders')
OG_DIR     = os.path.join(STATIC_DIR, 'og')

for d in (STATIC_DIR, UPLOAD_DIR, BANNER_DIR, HOT_DIR, ORDERS_DIR, OG_DIR):
    os.makedirs(d, exist_ok=True)

# OG-заглушка (чтобы ссылка og:image всегда была валидной)
DEFAULT_OG_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAeAAAAJ2CAYAAABm6r8dAAAACXBIWXMAAAsTAAALEwEAmpwY"
    "AAAAB3RJTUUH5QQUFzQ1qz7WgQAABi9JREFUeJzt3TEBwEAQwDDb/5y0QbIuN3gS8G6QkQAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAPhB4xkAAGl/3KQAAAAASUVORK5CYII="
)
OG_COVER_PATH = os.path.join(OG_DIR, 'cover.png')
if not os.path.exists(OG_COVER_PATH):
    try:
        with open(OG_COVER_PATH, 'wb') as f:
            f.write(base64.b64decode(DEFAULT_OG_PNG_B64))
    except Exception:
        pass

ALLOWED_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}

# Разрешаем крупные аплоады (до 50 МБ на объявление)
MAX_FILE_MB = 50
MAX_CONTENT_LENGTH = MAX_FILE_MB * 1024 * 1024

# -------------------- Flask/SocketIO --------------------
logging.getLogger('werkzeug').setLevel(logging.ERROR)
for name in ('engineio', 'socketio'):
    logging.getLogger(name).setLevel(logging.ERROR)

app = Flask(__name__, static_folder=BASE_DIR)
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# CORS: открыт, как и раньше
CORS(app, resources={r"/*": {"origins": "*"}})

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    logger=False,
    engineio_logger=False,
    ping_timeout=20,
    ping_interval=25
)

# -------------------- Утилиты/состояние --------------------
def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def default_state():
    return {
        "visitors": 27369,
        "seq": 51369,
        "banner": {"enabled": True, "image": "", "images": [], "link": "#"},
        "hot": [],
        "normal": [],
        "likes_by": {},
        "views_by": {},
        "pending": [],
        "seen_uids": {}
    }

def save_state(S):
    with open(os.path.join(BASE_DIR, DATA_FILE), 'w', encoding='utf-8') as f:
        f.write(json.dumps(S, ensure_ascii=False, indent=2))

def load_state():
    path = os.path.join(BASE_DIR, DATA_FILE)
    if not os.path.exists(path):
        S = default_state()
        save_state(S)
        return S
    try:
        S = json.load(open(path, 'r', encoding='utf-8'))
    except Exception:
        S = default_state()

    base = default_state()
    for k, v in base.items():
        if k not in S:
            S[k] = v

    for k in ("hot", "normal", "pending"):
        if not isinstance(S.get(k), list):
            S[k] = []
    for k in ("likes_by", "views_by", "seen_uids"):
        if not isinstance(S.get(k), dict):
            S[k] = {}
    if not isinstance(S.get("seq"), int):
        S["seq"] = 51369
    if not isinstance(S.get("banner"), dict):
        S["banner"] = base["banner"]
    if "images" not in S["banner"]:
        S["banner"]["images"] = []
    save_state(S)
    return S

S = load_state()

def base_url():
    try:
        return request.host_url.rstrip('/')
    except Exception:
        return f"http://127.0.0.1:{PORT}"

def abs_url(u: str) -> str:
    if not u:
        return ""
    if u.startswith('http'):
        return u
    return f"{base_url()}{u}"

def scan_banner_dir():
    files = [f for f in os.listdir(BANNER_DIR) if f.lower().endswith(tuple(ALLOWED_EXTS))]
    files.sort()
    return [f"/static/banners/{f}" for f in files]

def banner_payload():
    imgs = scan_banner_dir()
    img = imgs[0] if imgs else ""
    return {
        "enabled": True,
        "image": abs_url(img) if img else "",
        "images": [abs_url(x) for x in imgs],
        "link": S["banner"].get("link", "#")
    }

def refresh_banner(push=True):
    if push:
        socketio.emit('banner', banner_payload())

def find_ad(aid_or_code):
    for a in S["hot"]:
        if a["id"] == aid_or_code or str(a.get("code")) == str(aid_or_code):
            return a
    for a in S["normal"]:
        if a["id"] == aid_or_code or str(a.get("code")) == str(aid_or_code):
            return a
    return None

def purge_expired():
    now = now_ms()
    S["hot"]    = [a for a in S["hot"]    if a.get("activeTill", now + 1) > now]
    S["normal"] = [a for a in S["normal"] if a.get("activeTill", now + 1) > now]
    save_state(S)

def broadcast():
    socketio.emit('listings', {"hot": S["hot"], "normal": S["normal"]})

def push_visitors():
    socketio.emit('visitors', S["visitors"])

# -------------------- Глобальные заголовки/кэш --------------------
@app.after_request
def add_headers(resp):
    # CORS
    resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*') or '*'
    resp.headers['Access-Control-Allow-Credentials'] = 'true'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-KOLO-UID, Authorization, X-Requested-With'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'

    # Без компромиссов по качеству изображений: никаких трансформаций на сервере.
    # Укрепим кэш и политику реферера, чтобы внешние CDN всегда отдавали «как есть».
    resp.headers['Referrer-Policy'] = 'no-referrer'
    # Разрешаем картинки с любых доменов, остальное — по умолчанию
    resp.headers['Content-Security-Policy'] = "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: *; img-src 'self' data: blob: * https: http:; media-src * data: blob:; connect-src *;"

    # Кэширование
    if request.path in ('/', '/index.html'):
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
    elif request.path.startswith('/static/'):
        # 1 год + immutable, чтобы браузер не дёргал повторно
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    else:
        # По-умолчанию короткий кэш
        resp.headers.setdefault('Cache-Control', 'public, max-age=60')

    # Без MIME-угадываний
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp

def render_index():
    index_path = os.path.join(BASE_DIR, 'index.html')
    with open(index_path, 'r', encoding='utf-8') as f:
        html = f.read()
    b = base_url()
    html = html.replace('{{BASE}}', b)
    html = html.replace('{{OG_IMAGE}}', f"{b}/static/og/cover.png")
    html = html.replace('{{OG_URL}}', f"{b}/")
    html = html.replace('{{CANONICAL}}', f"{b}/")
    return html

# -------------------- Страницы/статика --------------------
@app.route('/', methods=['GET', 'HEAD'])
def root():
    return Response(render_index(), mimetype='text/html; charset=utf-8')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(BASE_DIR, path)

@app.route('/static/uploads/<path:name>')
def up(name):
    return send_from_directory(UPLOAD_DIR, name, as_attachment=False)

@app.route('/static/banners/<path:name>')
def up_banner(name):
    return send_from_directory(BANNER_DIR, name, as_attachment=False)

@app.route('/static/hot/<path:name>')
def up_hot(name):
    return send_from_directory(HOT_DIR, name, as_attachment=False)

@app.route('/static/orders/<path:name>')
def up_orders(name):
    return send_from_directory(ORDERS_DIR, name, as_attachment=False)

@app.route('/robots.txt')
def robots():
    return Response("User-agent: *\nAllow: /\n", mimetype='text/plain')

# -------------------- Пошук/нормализация --------------------
CYR_TO_LAT = {
    'а':'a','б':'b','в':'v','г':'h','ґ':'g','д':'d','е':'e','є':'ye','ж':'zh','з':'z','и':'y','і':'i','ї':'yi','й':'i',
    'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts','ч':'ch',
    'ш':'sh','щ':'shch','ь':'','ю':'yu','я':'ya','ё':'yo','э':'e','ы':'y'
}
def strip_accents(s: str) -> str:
    import unicodedata as u
    return ''.join(ch for ch in u.normalize('NFD', s) if not u.combining(ch))
def norm(s: str) -> str:
    s = (s or '')
    s = strip_accents(s).lower()
    s = re.sub(r'\s+', ' ', s).strip()
    return s
def translit_cyr_to_lat(s: str) -> str:
    return ''.join(CYR_TO_LAT.get(ch, ch) for ch in s.lower())
def match_query(q: str, *fields):
    qn = norm(q)
    if not qn:
        return True
    qn2 = translit_cyr_to_lat(qn)
    for f in fields:
        f0 = norm(f)
        f1 = norm(translit_cyr_to_lat(f))
        if qn in f0 or qn2 in f0 or qn in f1 or qn2 in f1:
            return True
    return False

# -------------------- API --------------------
@app.route('/api/list', methods=['GET', 'OPTIONS'])
def api_list():
    if request.method == 'OPTIONS':
        return ("", 204)
    purge_expired()
    return jsonify({"ok": True, "data": {"hot": S["hot"], "normal": S["normal"]}, "banner": banner_payload()})

@app.route('/api/search', methods=['GET', 'OPTIONS'])
def api_search():
    if request.method == 'OPTIONS':
        return ("", 204)
    purge_expired()
    q = request.args.get('q', '')
    district = (request.args.get('district') or '').strip()
    band = (request.args.get('price_band') or '').strip()
    kind = (request.args.get('kind') or '').strip()
    rooms = (request.args.get('rooms') or '').strip()

    def price_ok(price: int) -> bool:
        if not band:
            return True
        try:
            price = int(price or 0)
        except Exception:
            price = 0
        if band.endswith('+'):
            try:
                return price > int(band[:-1])
            except Exception:
                return True
        else:
            try:
                return price <= int(band)
            except Exception:
                return True

    def filt(arr):
        res = []
        for a in arr:
            if not match_query(q, a.get("title", ""), a.get("desc", ""), a.get("code", ""), a.get("phone", "")):
                continue
            if district and norm(district) != norm(a.get("district", "")):
                continue
            if kind and norm(kind) != norm(a.get("kind", "")):
                continue
            if rooms:
                try:
                    if int(a.get("rooms", 0)) != int(rooms):
                        continue
                except Exception:
                    continue
            if not price_ok(a.get("price", 0)):
                continue
            res.append(a)
        return res

    hot = filt(S["hot"])
    normal = filt(S["normal"])
    return jsonify({"ok": True, "data": {"hot": hot, "normal": normal}})

@app.route('/api/view/<aid>', methods=['POST', 'OPTIONS'])
def api_view(aid):
    if request.method == 'OPTIONS':
        return ("", 204)
    uid = request.headers.get('X-KOLO-UID', '')
    a = find_ad(aid)
    if a:
        last = S["views_by"].setdefault(aid, {}).get(uid, 0)
        if now_ms() - last > 10 * 60 * 1000:
            a["views"] = int(a.get("views", 0)) + 1
            S["views_by"][aid][uid] = now_ms()
            save_state(S)
            broadcast()
    return ("", 204)

@app.route('/api/like/<aid>', methods=['POST', 'OPTIONS'])
def api_like(aid):
    if request.method == 'OPTIONS':
        return ("", 204)
    uid = request.headers.get('X-KOLO-UID', '')
    a = find_ad(aid)
    if not a:
        return jsonify({"likes": 0, "liked": False})
    L = S["likes_by"].setdefault(aid, [])
    if uid and uid not in L:
        L.append(uid)
        a["likes"] = int(a.get("likes", 0)) + 1
        save_state(S)
        broadcast()
    return jsonify({"likes": a["likes"], "liked": uid in L})

# ---- CREATE => pending (+ детальный лог заявки пользователя)
@app.route('/api/create', methods=['POST', 'OPTIONS'])
def api_create():
    if request.method == 'OPTIONS':
        return ("", 204)

    kind = (request.form.get('type') or 'normal').lower()
    title = (request.form.get('title') or 'Оголошення')[:140]
    district = request.form.get('district') or ""
    desc = request.form.get('desc') or ""
    phone = request.form.get('phone') or "+380"
    rooms = (request.form.get('rooms') or "").strip()
    prop_kind = (request.form.get('kind') or "").strip()

    def to_price(val):
        try:
            return int(float(re.sub(r'[^0-9.,]', '', str(val or 0)).replace(',', '.')))
        except Exception:
            return 0

    price = to_price(request.form.get('price'))

    order_files = []
    order_files_meta = []
    if 'images' in request.files:
        files = request.files.getlist('images')
        files = files[:1] if kind == 'banner' else files
        for f in files:
            orig_name = secure_filename(f.filename or '')
            ext = os.path.splitext(orig_name)[1].lower() or '.jpg'
            if ext not in ALLOWED_EXTS:
                ext = '.jpg'
            saved_name = f"ord_{now_ms()}_{random.randint(1000, 9999)}{ext}"
            path = os.path.join(ORDERS_DIR, saved_name)
            # ВАЖНО: сохраняем как есть, без ресайза/перекодирования — максимум качества
            f.save(path)
            rel = f"/static/orders/{saved_name}"
            order_files.append(rel)
            order_files_meta.append({"orig": orig_name, "saved": saved_name, "url": abs_url(rel)})

    code = str(S.get("seq", 51369)).zfill(5)
    S["seq"] = S.get("seq", 51369) + 1
    days = 30 if kind == 'hot' else 30
    amount = 999 if kind == 'banner' else (299 if kind == 'hot' else 39)

    pending = {
        "code": code,
        "kind": kind,
        "amount": amount,
        "data": {
            "id": f"ad_{now_ms()}_{random.randint(1000, 9999)}",
            "code": code,
            "type": ("hot" if kind == 'hot' else "normal"),
            "title": title,
            "price": price,
            "district": district,
            "kind": prop_kind,
            "rooms": rooms,
            "desc": desc,
            "phone": phone,
            "images": [],
            "likes": 0,
            "views": 0,
            "activeTill": (datetime.now(timezone.utc) + timedelta(days=days)).timestamp() * 1000
        },
        "order_files": order_files,
        "order_files_meta": order_files_meta
    }
    S["pending"] = [p for p in S["pending"] if p.get("code") != code] + [pending]
    save_state(S)

    print(f"[PENDING] kind={kind} code={code} amount={amount}")
    print(f"        title={title}")
    print(f"        desc={desc}")
    print(f"        district={district}  rooms={rooms}  prop_kind={prop_kind}")
    print(f"        phone={phone}  price={price}")
    if order_files_meta:
        print("        images:")
        for i, m in enumerate(order_files_meta, 1):
            print(f"          {i}. orig='{m['orig']}' -> saved='{m['saved']}' url={m['url']}")
    else:
        print("        images: none")

    return jsonify({"ok": True, "kind": kind, "code": code, "amount": amount, "title": title})

@app.route('/api/order', methods=['POST'])
def api_order():
    try:
        j = request.get_json(force=True)
        code = j.get('code')
        kind = j.get('kind')
        amount = j.get('amount')
        P = [p for p in S.get("pending", []) if p.get("code") == code]
        if P:
            p = P[0]
            data = p.get("data", {})
            print(f"[ORDER] kind={kind} code={code} amount={amount} title={data.get('title','')}")
            print(f"        publish: district={data.get('district','')}, rooms={data.get('rooms','')}, kind={data.get('kind','')}, price={data.get('price',0)}, phone={data.get('phone','')}")
            if p.get("order_files_meta"):
                print("        images in order:")
                for i, m in enumerate(p["order_files_meta"], 1):
                    print(f"          {i}. orig='{m['orig']}' -> saved='{m['saved']}' url={abs_url('/static/orders/'+m['saved'])}")
        else:
            print(f"[ORDER] kind={kind} code={code} amount={amount} (pending not found)")
    except Exception as e:
        print("[ORDER-ERR]", e)
    return ("", 204)

# ---- Підтримка
@app.route('/api/support', methods=['POST'])
def api_support():
    try:
        j = request.get_json(force=True)
        print(f"[SUPPORT] name={j.get('name','')} phone={j.get('phone','')} msg={j.get('msg','')}")
    except Exception as e:
        print("[SUPPORT-ERR]", e)
    return jsonify({"ok": True})

# ---- Логи подій з клієнта
@app.route('/api/log', methods=['POST'])
def api_log():
    try:
        j = request.get_json(force=True)
        uid = request.headers.get('X-KOLO-UID', '')
        ip = request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For', '').split(',')[0] or request.remote_addr
        print(f"[EVENT] uid={uid} ip={ip} action={j.get('action')} extra={j.get('extra',{})}")
    except Exception as e:
        print("[EVENT-ERR]", e)
    return ("", 204)

# -------------------- Socket.IO (polling) --------------------
@socketio.on('connect')
def on_connect(auth):
    uid = (auth or {}).get('uid', '') if isinstance(auth, dict) else ''
    try:
        ua = request.headers.get('User-Agent', '')
        ip = request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For', '').split(',')[0] or request.remote_addr
        if uid and uid not in S["seen_uids"]:
            print(f"[VISIT] uid={uid} ip={ip} ua={ua[:140]}")
            S["seen_uids"][uid] = now_ms()
            save_state(S)
    except Exception as e:
        print("[VISIT-LOG-ERR]", e)
    S["visitors"] = int(S.get("visitors", 0)) + 1
    save_state(S)
    emit('visitors', S["visitors"])
    emit('banner', banner_payload())
    emit('listings', {"hot": S["hot"], "normal": S["normal"]})

def tick_visitors():
    while True:
        S["visitors"] = int(S.get("visitors", 0)) + random.randint(1, 3)
        save_state(S)
        push_visitors()
        time.sleep(15)

threading.Thread(target=tick_visitors, daemon=True).start()

# -------------------- Admin консоль --------------------
HELP = """
Admin:
  help | list | count | reset
  export <path.json> | import <path.json>
  setvis <N> | inc <N>

  # pending
  pend
  pub <code>
  reject <code>

  # banners
  bscan | blink <URL|#> | bclear | bshow | baddlocal <path> | bdel <filename>

  # ads
  add t|district|price|phone|rooms|kind|desc|imageURL[,imageURL2,...]
  addnorm ... | addhot ...
  delcode <5digits>
  addviews <id|code> <N> | addlikes <id|code> <N>
"""
def admin_console():
    print(HELP)
    for line in sys.stdin:
        s = line.strip()
        if not s:
            continue
        try:
            if s == "help":
                print(HELP)

            elif s == "list":
                for a in S["hot"] + S["normal"]:
                    print(a["id"], "-", f"[{a.get('code','-----')}]", "-", a["title"])

            elif s == "count":
                print("hot:", len(S["hot"]), "normal:", len(S["normal"]), "pending:", len(S["pending"]))

            elif s == "reset":
                S["hot"] = []
                S["normal"] = []
                S["likes_by"] = {}
                S["views_by"] = {}
                save_state(S)
                broadcast()
                print("[RESET] done")

            elif s == "pend":
                for p in S["pending"]:
                    print(f"[PENDING] {p['kind']} [{p['code']}] {p['data'].get('title','')} files={len(p.get('order_files',[]))}")

            elif s.startswith("pub "):
                code = s.split(" ", 1)[1].strip()
                P = [p for p in S["pending"] if p["code"] == code]
                if not P:
                    print("no pending")
                    continue
                p = P[0]

                if p["kind"] == "banner":
                    for rel in p.get("order_files", []):
                        name = os.path.basename(rel)
                        src = os.path.join(ORDERS_DIR, name)
                        if not os.path.isfile(src):
                            continue
                        dst = os.path.join(BANNER_DIR, name)
                        shutil.move(src, dst)
                    refresh_banner(push=True)
                    print("[PUBLISHED] banner", code)
                else:
                    ad = p["data"]
                    ad_images = []
                    for rel in p.get("order_files", []):
                        name = os.path.basename(rel)
                        src = os.path.join(ORDERS_DIR, name)
                        if not os.path.isfile(src):
                            continue
                        if ad["type"] == "hot":
                            dst = os.path.join(HOT_DIR, name)
                            shutil.move(src, dst)
                            ad_images.append(f"/static/hot/{name}")
                        else:
                            dst = os.path.join(UPLOAD_DIR, name)
                            shutil.move(src, dst)
                            ad_images.append(f"/static/uploads/{name}")
                    if not ad_images:
                        ad_images = ["https://picsum.photos/seed/new/1200/800"]
                    ad["images"] = ad_images
                    (S["hot"] if ad["type"] == "hot" else S["normal"]).insert(0, ad)
                    save_state(S)
                    broadcast()
                    print("[PUBLISHED]", ad["type"], code, "images:", len(ad_images))

                S["pending"] = [x for x in S["pending"] if x["code"] != code]
                save_state(S)

            elif s.startswith("reject "):
                code = s.split(" ", 1)[1].strip()
                S["pending"] = [x for x in S["pending"] if x["code"] != code]
                save_state(S)
                print("[REJECTED]", code)

            elif s == "bscan":
                refresh_banner(push=True)
                print("[BSCAN]")

            elif s.startswith("blink "):
                link = s.split(" ", 1)[1].strip() or "#"
                S["banner"]["link"] = link
                save_state(S)
                refresh_banner(push=True)
                print("[BLINK]", link)

            elif s == "bclear":
                for f in os.listdir(BANNER_DIR):
                    try:
                        os.remove(os.path.join(BANNER_DIR, f))
                    except Exception:
                        pass
                refresh_banner(push=True)
                print("[BCLEAR]")

            elif s == "bshow":
                print("[BSHOW]", [abs_url(u) for u in scan_banner_dir()])

            elif s.startswith("baddlocal "):
                src = s.split(" ", 1)[1]
                if not os.path.isfile(src):
                    print("no file:", src)
                    continue
                ext = os.path.splitext(src)[1].lower()
                if ext not in ALLOWED_EXTS:
                    print("unsupported ext")
                    continue
                dst = os.path.join(BANNER_DIR, f"bn_{now_ms()}{ext}")
                shutil.copyfile(src, dst)
                refresh_banner(push=True)
                print("[BADDLOCAL]->", dst)

            elif s.startswith("bdel "):
                name = s.split(" ", 1)[1]
                path = os.path.join(BANNER_DIR, name)
                if os.path.isfile(path):
                    os.remove(path)
                    refresh_banner(push=True)
                    print("[BDEL]")
                else:
                    print("no such banner file")

            elif s.startswith(("addnorm ", "addhot ", "add ")):
                kind = 'normal' if s.startswith(("addnorm ", "add ")) else 'hot'
                _, payload = s.split(" ", 1)
                parts = payload.split("|")
                if len(parts) < 8:
                    print("usage:", HELP)
                    continue
                title, district, price, phone, rooms, prop_kind, desc, imgcsv = parts[:8]
                imgs = [u.strip() for u in imgcsv.split(",") if u.strip()]
                code = str(S.get("seq", 51369)).zfill(5)
                S["seq"] = S.get("seq", 51369) + 1
                days = 30 if kind == 'hot' else 30
                ad = {
                    "id": f"ad_{now_ms()}_{random.randint(1000, 9999)}",
                    "code": code,
                    "type": ('hot' if kind == 'hot' else 'normal'),
                    "title": title,
                    "price": int(float(price or 0)),
                    "district": district,
                    "phone": phone,
                    "rooms": rooms,
                    "kind": prop_kind,
                    "desc": desc,
                    "images": (imgs if imgs else ["https://picsum.photos/seed/new/1200/800"]),
                    "likes": 0,
                    "views": 0,
                    "activeTill": (datetime.now(timezone.utc) + timedelta(days=days)).timestamp() * 1000
                }
                (S["hot"] if kind == 'hot' else S["normal"]).insert(0, ad)
                save_state(S)
                broadcast()
                print(f"[ADD-{kind.upper()}] {title} [{code}] imgs:{len(imgs)}")

            elif s.startswith("delcode "):
                code = s.split(" ", 1)[1].strip()
                before = (len(S["hot"]) + len(S["normal"]))
                S["hot"]   = [a for a in S["hot"]   if str(a.get("code")) != code]
                S["normal"] = [a for a in S["normal"] if str(a.get("code")) != code]
                save_state(S)
                broadcast()
                after = (len(S["hot"]) + len(S["normal"]))
                print("[DELCODE]", code, "removed:", before - after)

            elif s.startswith("addviews "):
                _, rest = s.split(" ", 1)
                tgt, n = rest.split()
                ad = find_ad(tgt)
                if not ad:
                    print("not found")
                    continue
                ad["views"] = int(ad.get("views", 0)) + int(n)
                save_state(S)
                broadcast()
                print("[ADDVIEWS]", tgt, "+", n)

            elif s.startswith("addlikes "):
                _, rest = s.split(" ", 1)
                tgt, n = rest.split()
                ad = find_ad(tgt)
                if not ad:
                    print("not found")
                    continue
                ad["likes"] = int(ad.get("likes", 0)) + int(n)
                save_state(S)
                broadcast()
                print("[ADDLIKES]", tgt, "+", n)

            else:
                print("unknown. type 'help'")
        except Exception as e:
            print("[ERR]", e)

threading.Thread(target=admin_console, daemon=True).start()

# -------------------- Запуск --------------------
if __name__ == '__main__':
    print('Admin console ready. Type: help')
    print(f'ХАТА© Python server on {base_url()}')
    # allow_unsafe_werkzeug=True — как и раньше для простоты локального запуска
    socketio.run(app, host='0.0.0.0', port=PORT, allow_unsafe_werkzeug=True)
