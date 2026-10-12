#!/usr/bin/env python3
"""
Agent "dons Leboncoin" -> notifications Discord

Surveille la catégorie DONS de Leboncoin autour de chez toi, filtre les annonces
avec Claude (revendable ? fonctionnel ? marge possible ?) et t'envoie sur Discord
le lien + une estimation de revente + un message prêt à copier-coller.

Usage :
    python bot.py            # boucle infinie (Railway / Raspberry Pi)
    python bot.py --once     # un seul passage (test)
    python bot.py --test     # envoie un message de test sur Discord

Variables d'environnement : voir README.md
"""
import json
import logging
import math
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import lbc
import requests

# =====================================================================
# CONFIG (modifiable ici, ou via variables d'environnement)
# =====================================================================

# Point de départ : Yutz (57). Change si besoin.
HOME_NAME = "Yutz"
HOME_LAT = float(os.getenv("HOME_LAT", "49.358"))
HOME_LNG = float(os.getenv("HOME_LNG", "6.175"))

# Rayon : gros objets = on va plus loin (Thionville -> Metz), petits = plus proche
RADIUS_BIG_KM = int(os.getenv("RADIUS_BIG_KM", "35"))
RADIUS_SMALL_KM = int(os.getenv("RADIUS_SMALL_KM", "20"))

POLL_MINUTES = float(os.getenv("POLL_MINUTES", "6"))   # pause entre deux passages
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "72"))   # ignore les annonces plus vieilles
MIN_SCORE = int(os.getenv("MIN_SCORE", "5"))            # score Claude minimum (0-10)

# Phrase de disponibilité insérée dans les messages (à adapter)
AVAILABILITY = os.getenv("AVAILABILITY", "Je peux passer très rapidement")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1559109020776927244/7fEr_Mg4fHsFxTqv-U3K5lrZYPnlIbvesMBiuyNi_X-x687osH58xDEAhwixYGQfb6ZX")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
PROXY_URL = os.getenv("PROXY_URL", "")  # ex: http://user:pass@host:port (optionnel)
DB_PATH = os.getenv("DB_PATH", "seen.db")  # sur Railway : /data/seen.db (volume)
DRY_RUN = os.getenv("DRY_RUN", "") == "1"  # affiche au lieu d'envoyer sur Discord

# Mode "scan" : au lieu de chercher des mots-clés (beaucoup de requêtes, angles morts),
# on lit TOUS les dons récents autour de chez toi (peu de requêtes) et c'est Claude qui
# trie. Activé automatiquement si une clé API est fournie.
SCAN_ALL = os.getenv("SCAN_ALL", "1" if ANTHROPIC_API_KEY else "0") == "1"
SCAN_PAGES = int(os.getenv("SCAN_PAGES", "2"))  # 35 annonces par page

# Les recherches. Chaque mot-clé est cherché dans le TITRE des annonces de la
# catégorie "Dons". Ajoute / retire des mots-clés librement.
SEARCHES = [
    {
        "name": "Jouets / enfants",
        "radius_km": RADIUS_SMALL_KM,
        "keywords": ["lego", "playmobil", "duplo", "jouets", "jeux enfant", "circuit",
                     "train électrique", "jeu de société", "vélo enfant", "draisienne",
                     "trottinette"],
    },
    {
        "name": "Puériculture de marque",
        "radius_km": RADIUS_BIG_KM,
        "keywords": ["poussette", "lit bébé", "parc bébé", "transat", "chaise haute",
                     "cybex", "bugaboo"],
    },
    {
        "name": "Gaming / tech Apple",
        "radius_km": RADIUS_SMALL_KM,
        "keywords": ["console", "playstation", "xbox", "nintendo", "manette",
                     "retrogaming", "game boy", "jeux vidéo", "iphone", "ipad",
                     "macbook", "airpods", "écran pc", "pc portable"],
    },
    {
        "name": "Sport / vélos",
        "radius_km": RADIUS_BIG_KM,
        "keywords": ["vélo d'appartement", "home trainer", "tapis de course", "rameur",
                     "banc de musculation", "haltères", "vélo de route", "vtt",
                     "trottinette électrique", "vélo électrique"],
    },
    {
        "name": "Maison / meubles",
        "radius_km": RADIUS_BIG_KM,
        "keywords": ["ikea", "table basse", "buffet", "commode", "fauteuil",
                     "bureau", "vinyle", "machine à café", "nespresso", "thermomix"],
    },
    {
        "name": "Outils",
        "radius_km": RADIUS_SMALL_KM,
        "keywords": ["perceuse", "visseuse", "makita", "bosch", "dewalt", "meuleuse",
                     "karcher"],
    },
]

# Objets jamais intéressants (mode scan) : évite d'appeler Claude pour rien
SKIP_WORDS = re.compile(
    r"\b(gravats?|bois de chauffage|palettes?|boutures?|compost|fumier|terre v[ée]g[ée]tale|"
    r"chaton|chiot|poules?|lapins?)\b", re.IGNORECASE)

# Mots qui disqualifient une annonce (matériel HS, à réparer, etc.)
BAD_WORDS = re.compile(
    r"\b(hs|hors service|pour pi[eè]ces|ne (?:fonctionne|marche) (?:pas|plus)|"
    r"en panne|[àa] r[ée]parer|cass[ée]e?s?|d[ée]fectueu[xs]e?s?)\b",
    re.IGNORECASE,
)

PARIS = ZoneInfo("Europe/Paris")
log = logging.getLogger("dons")

# =====================================================================
# BASE DE DONNÉES (annonces déjà vues)
# =====================================================================

def open_db() -> sqlite3.Connection:
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen (id INTEGER PRIMARY KEY, ts TEXT)")
    db.commit()
    return db


def is_seen(db, ad_id) -> bool:
    return db.execute("SELECT 1 FROM seen WHERE id=?", (ad_id,)).fetchone() is not None


def mark_seen(db, ad_id) -> None:
    db.execute("INSERT OR IGNORE INTO seen (id, ts) VALUES (?, ?)",
               (ad_id, datetime.now().isoformat()))
    db.commit()

# =====================================================================
# OUTILS
# =====================================================================

def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ad_age_hours(ad):
    try:
        dt = datetime.fromisoformat(str(ad.first_publication_date))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PARIS)
        return (datetime.now(PARIS) - dt) / timedelta(hours=1)
    except Exception:
        return None  # date illisible -> on garde l'annonce


def make_client():
    proxy = None
    if PROXY_URL:
        u = urlparse(PROXY_URL)
        proxy = lbc.Proxy(host=u.hostname, port=u.port, username=u.username,
                          password=u.password, scheme=u.scheme or "http")
    return lbc.Client(proxy=proxy) if proxy else lbc.Client()

# =====================================================================
# SCORING CLAUDE
# =====================================================================

_anthropic = None

SYSTEM_PROMPT = """Tu aides un particulier en France à repérer, parmi des annonces de DONS \
sur Leboncoin, les objets qu'il pourra revendre d'occasion avec un petit bénéfice \
(Leboncoin, Vinted, Facebook Marketplace). Il ne veut que des objets FONCTIONNELS.

Réponds UNIQUEMENT par un objet JSON (sans texte autour, sans balises) avec ces clés :
- "revendable": true/false (y a-t-il un marché réaliste d'occasion ?)
- "etat": "fonctionne" | "incertain" | "hs" (d'après le texte de l'annonce)
- "score": entier de 0 à 10 (potentiel de marge, en tenant compte de l'état, de la \
demande, de l'encombrement et de la facilité de transport/envoi)
- "prix_min": prix de revente réaliste bas, en euros (entier)
- "prix_max": prix de revente réaliste haut, en euros (entier)
- "encombrement": "petit" | "moyen" | "gros"
- "delai_vente": "rapide" (< 2 semaines) | "moyen" (2-6 semaines) | "lent" (> 6 semaines)
- "resume": une phrase courte en français (modèle probable, point d'attention)
- "message": message court, poli, en français, pour contacter le donneur \
(demander si l'objet est toujours disponible et fonctionnel, proposer de passer ; \
sans mentionner la revente). Termine par la phrase de disponibilité fournie.

Repères : se revendent vite = Lego/Playmobil, consoles et jeux, produits Apple, outils \
électroportatifs de marque, poussettes/puériculture de marque, vélos (enfant, route, \
électriques), haltères/bancs de muscu, meubles IKEA ou design en bon état, vinyles, \
machines à café/robots de marque. Lents ou peu rentables = télés, imprimantes, gros \
électroménager, canapés usés, meubles lourds sans marque, vêtements isolés, livres \
isolés, tout ce qui est vieux ou démodé. Les objets saisonniers dépendent de la date \
(fitness en janvier, jouets avant Noël, vélos au printemps) : tiens compte de la période \
actuelle. Le donneur n'a pas fixé de prix : un objet qui mettrait des mois à partir doit \
avoir un score bas (<= 4). Au-delà de 20 km, ne donne un score élevé que si le gain \
justifie clairement le trajet. Les sièges auto sont à rejeter (sécurité, péremption).

Sois réaliste et prudent sur les prix : mieux vaut sous-estimer. Si l'annonce est \
trop vague pour juger, mets etat "incertain" et un score moyen (4-5)."""


def score_ad(ad, search_name: str, dist_km):
    """Retourne un dict de scoring, ou None si indisponible / erreur."""
    global _anthropic
    if not ANTHROPIC_API_KEY:
        return None
    try:
        if _anthropic is None:
            import anthropic
            _anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        loc = ad.location.city_label if ad.location else "?"
        dist = f"{dist_km:.0f} km" if dist_km is not None else "?"
        body = (ad.body or "")[:1200]
        user = (f"Recherche : {search_name}\nTitre : {ad.subject}\n"
                f"Description : {body}\nLieu : {loc} ({dist} de chez moi)\n"
                f"Phrase de disponibilité : {AVAILABILITY}\n"
                f"Date du jour : {datetime.now(PARIS).strftime('%d/%m/%Y')}")
        resp = _anthropic.messages.create(
            model=CLAUDE_MODEL, max_tokens=500, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(text)
    except Exception as e:
        log.warning("Scoring Claude échoué (%s) : %s", ad.id, e)
        return None

# =====================================================================
# DISCORD
# =====================================================================

def post_discord(payload: dict) -> None:
    if DRY_RUN or not DISCORD_WEBHOOK_URL:
        print("[DISCORD]", json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for _ in range(3):
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.status_code >= 300:
            log.error("Discord %s : %s", r.status_code, r.text[:200])
        return


def notify(ad, search_name, dist_km, sc) -> None:
    loc = ad.location.city_label if ad.location else "?"
    dist = f"{dist_km:.0f} km" if dist_km is not None else "?"
    fields = [{"name": "📍 Lieu", "value": f"{loc} ({dist})", "inline": True},
              {"name": "🏷️ Recherche", "value": search_name, "inline": True}]
    if sc:
        score = int(sc.get("score", 0))
        color = 0x2ECC71 if score >= 7 else 0xF1C40F
        fields += [
            {"name": "⭐ Score", "value": f"{score}/10", "inline": True},
            {"name": "💶 Revente estimée",
             "value": f"{sc.get('prix_min', '?')}–{sc.get('prix_max', '?')} €", "inline": True},
            {"name": "🔧 État", "value": str(sc.get("etat", "?")), "inline": True},
            {"name": "📦 Taille", "value": str(sc.get("encombrement", "?")), "inline": True},
            {"name": "⏱️ Vente", "value": str(sc.get("delai_vente", "?")), "inline": True},
        ]
        desc = str(sc.get("resume", ""))
        msg = str(sc.get("message", "")).strip()
        if msg:
            desc += f"\n\n**Message prêt à envoyer :**\n```{msg[:900]}```"
    else:
        color = 0x95A5A6
        desc = "(non évalué par Claude)\n\n" + (ad.body or "")[:600]

    embed = {"title": (ad.subject or "Annonce")[:250], "url": ad.url,
             "description": desc[:3800], "color": color, "fields": fields}
    if ad.images:
        embed["thumbnail"] = {"url": ad.images[0]}
    post_discord({"embeds": [embed]})

# =====================================================================
# BOUCLE PRINCIPALE
# =====================================================================

_consecutive_failures = 0
_alert_sent = False


def register_failure(err: Exception) -> None:
    global _consecutive_failures, _alert_sent
    _consecutive_failures += 1
    log.warning("Erreur de recherche (%d de suite) : %s", _consecutive_failures, err)
    if _consecutive_failures >= 8 and not _alert_sent:
        _alert_sent = True
        post_discord({"content": "⚠️ L'agent n'arrive plus à interroger Leboncoin "
                                 "(blocage anti-bot probable). Essaie un PROXY_URL "
                                 "résidentiel ou héberge-le chez toi."})


def register_success() -> None:
    global _consecutive_failures, _alert_sent
    _consecutive_failures = 0
    _alert_sent = False


def build_queries() -> list:
    if SCAN_ALL:
        return [{"name": "Dons (scan)", "radius_km": RADIUS_BIG_KM, "kw": None, "page": p}
                for p in range(1, SCAN_PAGES + 1)]
    return [{"name": s["name"], "radius_km": s["radius_km"], "kw": kw, "page": 1}
            for s in SEARCHES for kw in s["keywords"]]


def run_cycle(client, db) -> int:
    sent = 0
    for q in build_queries():
        params = dict(
            category=lbc.Category.DONS,
            sort=lbc.Sort.NEWEST,
            locations=[lbc.City(lat=HOME_LAT, lng=HOME_LNG,
                                radius=q["radius_km"] * 1000, city=HOME_NAME)],
            limit=35,
            page=q["page"],
        )
        if q["kw"]:
            params.update(text=q["kw"], search_in_title_only=True)
        try:
            res = client.search(**params)
            register_success()
        except Exception as e:
            register_failure(e)
            time.sleep(random.uniform(5, 12))
            continue

        for ad in res.ads:
            if is_seen(db, ad.id):
                continue
            mark_seen(db, ad.id)

            age = ad_age_hours(ad)
            if age is not None and age > MAX_AGE_HOURS:
                continue
            text = f"{ad.subject} {ad.body or ''}"
            if BAD_WORDS.search(text) or (SCAN_ALL and SKIP_WORDS.search(ad.subject or "")):
                log.info("Rejeté (filtre mots) : %s", ad.subject)
                continue

            dist = None
            if ad.location and ad.location.lat is not None:
                dist = haversine_km(HOME_LAT, HOME_LNG, ad.location.lat, ad.location.lng)
                if dist > q["radius_km"] + 3:
                    continue

            sc = score_ad(ad, q["name"], dist)
            if sc is not None:
                if (not sc.get("revendable", True) or sc.get("etat") == "hs"
                        or int(sc.get("score", 0)) < MIN_SCORE):
                    log.info("Rejeté (score) : %s -> %s", ad.subject, sc.get("score"))
                    continue

            notify(ad, q["name"], dist, sc)
            sent += 1
        time.sleep(random.uniform(3, 8))  # pas de rafale de requêtes
    return sent


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if "--test" in sys.argv:
        post_discord({"content": "✅ Agent dons : connexion Discord OK."})
        return

    if not DISCORD_WEBHOOK_URL and not DRY_RUN:
        log.warning("DISCORD_WEBHOOK_URL absent : affichage console uniquement.")
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY absent : pas de scoring, toutes les annonces seront envoyées.")

    db = open_db()
    client = make_client()
    once = "--once" in sys.argv
    while True:
        try:
            n = run_cycle(client, db)
            log.info("Passage terminé : %d notification(s).", n)
        except Exception as e:  # ne jamais planter la boucle
            log.exception("Erreur inattendue : %s", e)
        if once:
            break
        time.sleep(POLL_MINUTES * 60 * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    main()
