#!/usr/bin/env python3
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- CHECK DEPENDENCIES ---
try:
    from DrissionPage import ChromiumPage, ChromiumOptions
except ImportError:
    print("::error::Missing dependencies. Please run: pip install DrissionPage", file=sys.stderr)
    sys.exit(1)


ACCOUNT = 226241953
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "borov.db"
OUT = ROOT / "data.json"
HISTORY_OUT = ROOT / "history.json"
HISTORY_TOP_HEROES = 12

SLEEP_SECONDS = 3 * 60 * 60  # 3 hours
BASE_TS, BASE_MMR = "2026-09-16T09:00:00+00:00", 6781
BASE_DATE = BASE_TS[:10]
HISTORY_FROM = "2026-07-31"
STEP = 25
KEEP = 20
SHAME = 10
SHAME_MIN_D = 10
FORM = 50
OD_PER_RUN = 40
OD_RETRY_DAYS = 3
REFILL_PER_RUN = 150
CDN = "https://cdn.cloudflare.steamstatic.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

STEAM_KEY = os.environ.get("STEAM_API_KEY")
STEAM = "https://api.steampowered.com/IDOTA2Match_570"

MODES = {1: "All Pick", 2: "Captains Mode", 3: "Random Draft", 4: "Single Draft", 5: "All Random", 16: "Captains Draft",
         18: "Ability Draft", 22: "All Pick", 23: "Turbo"}
RADIANCE = 137
SKIP_ITEMS = {"tpscroll", "branches", "ward_observer", "ward_sentry", "ward_dispenser", "dust", "smoke_of_deceit",
              "tango", "flask", "clarity", "faerie_fire", "enchanted_mango", "blood_grenade", "cheese", "aegis",
              "refresher_shard", "famango", "great_famango", "greater_famango"}

SCHEMA = """
create table if not exists matches (
  id integer primary key, start text not null, hero_id integer, hero_name text, win integer not null,
  type text, mode text, duration integer, k integer, d integer, a integer, side text,
  level integer, net_worth integer, gpm integer, xpm integer, lh integer, dn integer, hero_damage integer,
  items text, backpack text, neutral integer, purchased text, score text, allies text, enemies text,
  region integer, cluster integer, od_done integer default 0, od_tries integer default 0, source text
);
create table if not exists calendar (date text primary key, w integer, l integer);
create table if not exists kv (k text primary key, v text);
"""

def get(url, headers=None, timeout=60, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"GET {re.sub(r'key=[^&]+', 'key=***', url)} failed ({attempt + 1}/{tries}): {e}", file=sys.stderr)
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 402, 404):
                return None
            time.sleep(5 * (attempt + 1))
    return None

def get_json(url):
    body = get(url)
    try:
        return json.loads(body) if body else None
    except json.JSONDecodeError:
        return None

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r["name"] for r in db.execute("pragma table_info(matches)")}
    for col, typ in (("region", "integer"), ("cluster", "integer"), ("allies", "text"), ("enemies", "text")):
        if col not in cols:
            db.execute(f"alter table matches add column {col} {typ}")
    return db

def kv_get(db, k, default=None):
    r = db.execute("select v from kv where k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default

def kv_set(db, k, v):
    db.execute("insert or replace into kv(k, v) values(?, ?)", (k, json.dumps(v, ensure_ascii=False)))

def get_dotabuff_match_ids(pages=1):
    print(f"Fetching match IDs from Dotabuff ({pages} pages) via browser...")

    co = ChromiumOptions()
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--window-size=1280,800')
    co.set_user_data_path(str(ROOT / "data" / "chrome_profile"))
    co.set_argument('--disk-cache-size=52428800')

    page = None
    try:
        page = ChromiumPage(co)
        all_ids = []


        for p in range(1, pages + 1):
            progress(p, pages, "Dotabuff pages")
            target = "https://www.dotabuff" + ".com/players/" + str(ACCOUNT) + f"/matches?page={p}"
            page.get(target)
            time.sleep(3)
#Break to login
        # for p in range(1, pages + 1):
        #     target = "https://www.dotabuff" + ".com/players/" + str(ACCOUNT) + f"/matches?page={p}"
        #     print(f"  Loading page {p}/{pages}...")
        #     page.get(target)
        #     input("👉 Finish logging in with your burner Steam account, then press ENTER here...")


            links = page.eles('css:a[href^="/matches/"]')
            page_ids = []
            for link in links:
                href = link.attr('href') or ""
                mid = href.split('/')[-1]
                if mid.isdigit():
                    page_ids.append(int(mid))

            if not page_ids:
                print(f"  No matches found on page {p}, stopping pagination.")
                break

            all_ids.extend(page_ids)

        unique_ids = list(dict.fromkeys(all_ids))
        if unique_ids:
            print(f"✅ Found {len(unique_ids)} unique match IDs across {pages} pages!")
            return unique_ids

        print(f"❌ Page loaded (title: {page.title}), but no match links found.")
        return None

    except Exception as e:
        print(f"❌ Browser scrape error: {type(e).__name__} - {e}")
        return None
    finally:
        if page:
            page.quit()


def basic_row(d, heroes):
    p = next((p for p in (d or {}).get("players") or [] if p.get("account_id") == ACCOUNT), None)
    if not p or d.get("radiant_win") is None:
        return None
    return {
        "id": d["match_id"], "hero_name": heroes.get(str(p["hero_id"]), {}).get("name") or str(p["hero_id"]),
        "win": int(d["radiant_win"] == (p["player_slot"] < 128)),
        "start": datetime.fromtimestamp(d["start_time"], timezone.utc).isoformat(timespec="seconds"),
        "type": "Ranked" if d.get("lobby_type") == 7 else "Turbo" if d.get("game_mode") == 23 else "Unranked",
        "mode": MODES.get(d.get("game_mode")), "duration": d.get("duration"),
        "k": p.get("kills"), "d": p.get("deaths"), "a": p.get("assists"),
    }

def from_dotabuff(db, heroes):
    ids = get_dotabuff_match_ids()
    if ids is None:
        return None

    known = {r["id"] for r in db.execute("select id from matches")}
    new_ids = [i for i in ids if i not in known]

    if not new_ids:
        return 0

    added = 0
    total = len(new_ids)
    for i, mid in enumerate(new_ids, 1):
        progress(i, total, "Match details")
        od = get_json(f"https://api.opendota.com/api/matches/{mid}")
        row, source = basic_row(od, heroes), "opendota"

        if not row and STEAM_KEY:
            row, source = basic_row((get_json(f"{STEAM}/GetMatchDetails/v1/?key={STEAM_KEY}&match_id={mid}") or {}).get("result"), heroes), "steam"

        if not row:
            print(f"Waiting for details for match {mid} (OpenDota lag)", file=sys.stderr)
            continue

        upsert_basic(db, row, source)
        if source == "opendota":
            apply_opendota(db, mid, od)
        db.commit()
        added += 1
        time.sleep(1.5)

    return added

def upsert_basic(db, r, source):
    db.execute(
        """insert into matches(id, start, hero_name, win, type, mode, duration, k, d, a, source)
           values(:id, :start, :hero_name, :win, :type, :mode, :duration, :k, :d, :a, :source)
           on conflict(id) do update set hero_name=excluded.hero_name, win=excluded.win,
             type=coalesce(excluded.type, type), mode=coalesce(excluded.mode, mode),
             duration=coalesce(duration, excluded.duration), k=excluded.k, d=excluded.d, a=excluded.a""",
        {"type": None, "mode": None, "duration": None, **r, "source": source},
    )

def load_constants(db):
    const = kv_get(db, "const")
    if const and "regions" in const and const.get("ts", 0) > time.time() - 7 * 86400:
        return const
    heroes = get_json("https://api.opendota.com/api/constants/heroes")
    items = get_json("https://api.opendota.com/api/constants/items")
    regions = get_json("https://api.opendota.com/api/constants/region")
    if not (heroes and items and regions):
        return const or {"heroes": {}, "items": {}, "regions": {}}
    const = {
        "ts": int(time.time()),
        "regions": {k: region_name(v) for k, v in regions.items()},
        "heroes": {str(h["id"]): {"name": h["localized_name"], "img": CDN + h["img"].split("?")[0]} for h in heroes.values()},
        "items": {str(v["id"]): {"key": k, "name": v.get("dname") or k, "img": CDN + v["img"].split("?")[0]}
                  for k, v in items.items() if v.get("id") is not None and v.get("img")},
    }
    kv_set(db, "const", const)
    return const

def lineup(d, p):
    radiant = p["player_slot"] < 128
    allies = [x["hero_id"] for x in d["players"] if (x["player_slot"] < 128) == radiant and x is not p]
    enemies = [x["hero_id"] for x in d["players"] if (x["player_slot"] < 128) != radiant]
    return json.dumps(allies), json.dumps(enemies)

def apply_opendota(db, mid, d):
    if not d or not d.get("players"):
        return False
    p = next((p for p in d["players"] if p.get("account_id") == ACCOUNT), None)
    if not p:
        return False
    radiant = p["player_slot"] < 128
    score = [d.get("radiant_score"), d.get("dire_score")]
    purchased = sorted({x["key"] for x in (p.get("purchase_log") or [])}) or None
    db.execute(
        """update matches set hero_id=?, side=?, win=?, duration=?, level=?, net_worth=?, gpm=?, xpm=?, lh=?, dn=?,
             hero_damage=?, items=?, backpack=?, neutral=?, purchased=?, score=?, region=?, cluster=?, allies=?, enemies=?, od_done=1,
             k=?, d=?, a=?, type=coalesce(type, ?)
           where id=?""",
        (p.get("hero_id"), "radiant" if radiant else "dire", int(d["radiant_win"] == radiant), d.get("duration"),
         p.get("level"), p.get("net_worth"), p.get("gold_per_min"), p.get("xp_per_min"), p.get("last_hits"),
         p.get("denies"), p.get("hero_damage"),
         json.dumps([p.get(f"item_{i}") or 0 for i in range(6)]),
         json.dumps([p.get(f"backpack_{i}") or 0 for i in range(3)]),
         p.get("item_neutral") or 0,
         json.dumps(purchased) if purchased else None,
         json.dumps(score if radiant else score[::-1]),
         d["region"] if d.get("region") is not None else -1, d.get("cluster"), *lineup(d, p),
         p.get("kills"), p.get("deaths"), p.get("assists"),
         "Ranked" if d.get("lobby_type") == 7 else None,
         mid),
    )
    return True

def fill_details(db, limit):
    recent = (datetime.now(timezone.utc) - timedelta(days=OD_RETRY_DAYS)).isoformat(timespec="seconds")
    todo = db.execute(
        "select id from matches where od_done=0 and (od_tries<8 or start>=?) order by start desc limit ?",
        (recent, limit),
    ).fetchall()
    for r in todo:
        d = get_json(f"https://api.opendota.com/api/matches/{r['id']}")
        db.execute("update matches set od_tries=od_tries+1 where id=?", (r["id"],))
        apply_opendota(db, r["id"], d)
        db.commit()
        time.sleep(1.5)
    return len(todo)

def refill(db, limit):
    todo = db.execute(
        "select id from matches where od_done=1 and (region is null or region < 0 and cluster is null or enemies is null)"
        " and start >= ? order by start desc limit ?",
        (HISTORY_FROM, limit),
    ).fetchall()
    for r in todo:
        d = get_json(f"https://api.opendota.com/api/matches/{r['id']}")
        p = next((p for p in d["players"] if p.get("account_id") == ACCOUNT), None) if d and d.get("players") else None
        if p:
            db.execute("update matches set region=?, cluster=?, allies=?, enemies=? where id=?",
                       (d["region"] if d.get("region") is not None else -1, d.get("cluster"), *lineup(d, p), r["id"]))
            db.commit()
        time.sleep(1.5)
    return len(todo)

def main(od_limit=OD_PER_RUN):
    db = connect()
    const = load_constants(db)

    added = from_dotabuff(db, const["heroes"])
    if added is not None:
        status = "ok"
        print(f"dotabuff: {added} new matches")
    else:
        status = "unavailable"
        print("::warning::Match source unavailable, keeping previous data", file=sys.stderr)

    kv_set(db, "scrape_status", status)
    if status == "ok":
        kv_set(db, "scraped_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    profile = get_json("https://api.opendota" + ".com/api/players/" + str(ACCOUNT))
    if profile and profile.get("profile"):
        pr = profile["profile"]
        kv_set(db, "profile", {"name": pr.get("personaname"), "avatar": pr.get("avatarfull"),
                               "rank_tier": profile.get("rank_tier"), "leaderboard_rank": profile.get("leaderboard_rank")})
    db.commit()

    n = fill_details(db, od_limit)
    refilled = refill(db, REFILL_PER_RUN)
    print(f"opendota details tried: {n}")
    print(f"opendota refill tried: {refilled}")

    OUT.write_text(json.dumps(build_output(db, const), ensure_ascii=False, separators=(",", ":")))
    HISTORY_OUT.write_text(json.dumps(history_list(load_history(db, const["heroes"])), ensure_ascii=False, separators=(",", ":")))
    db.close()
    print("ok")

    if (added and added > 0) or n > 0 or refilled > 0:
        print("Checking for JSON changes to push...")
        subprocess.run(["git", "add", "data.json", "history.json"], cwd=ROOT, check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", f"chore: update matches (+{added or 0})"], cwd=ROOT, check=False)
            subprocess.run(["git", "push"], cwd=ROOT, check=False)
            print("Pushed updates to GitHub.")
        else:
            print("No JSON changes to push.")
    else:
        print("No new matches — skipping git push.")

def build_output(db, const):
    heroes, items = const["heroes"], const["items"]
    cal = {r["date"]: (r["w"], r["l"]) for r in db.execute("select * from calendar")}

    all_rows = [dict(r) for r in db.execute("select * from matches where start >= ? order by start desc", (HISTORY_FROM,))]
    for m in all_rows:
        for f in ("items", "backpack", "purchased", "score", "allies", "enemies"):
            m[f] = json.loads(m[f]) if m[f] else None
        hero = heroes.get(str(m["hero_id"]), {})
        m["hero"] = hero.get("name") or m["hero_name"]
        m["hero_img"] = hero.get("img") or f"{CDN}/apps/dota2/images/dota_react/heroes/{slug(m['hero_name'])}.png"

    days, current = rating_series(all_rows, cal)

    recent = []
    for m in all_rows[:KEEP]:
        recent.append({
            "id": m["id"], "start": m["start"], "hero": m["hero"], "hero_img": m["hero_img"],
            "win": bool(m["win"]), "type": m["type"], "duration": m["duration"],
            "k": m["k"], "d": m["d"], "a": m["a"],
            "nw": m["net_worth"], "gpm": m["gpm"], "xpm": m["xpm"], "lh": m["lh"], "dn": m["dn"],
            "level": m["level"], "side": m["side"], "score": m["score"],
            "items": [item_ref(items, i) for i in (m["items"] or [])],
            "neutral": item_ref(items, m["neutral"]),
            "delta": m["delta"], "mmr_after": m["mmr_after"],
        })

    shame = sorted((m for m in all_rows if (m["d"] or 0) >= SHAME_MIN_D),
                   key=lambda m: (-(m["d"] - m["k"]), -m["d"], m["start"]))[:SHAME]
    shame = [{f: m[f] for f in ("id", "start", "hero", "hero_img", "type", "duration", "k", "d", "a")} | {"win": bool(m["win"])}
             for m in shame]

    return {
        "account_id": ACCOUNT,
        "profile": kv_get(db, "profile", {}),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
	"next_update_at": (datetime.now(timezone.utc) + timedelta(seconds=SLEEP_SECONDS + 120)).isoformat(timespec="seconds"),
        "scraped_at": kv_get(db, "scraped_at"),
        "scrape_status": kv_get(db, "scrape_status"),
        "history_from": HISTORY_FROM,
        "step": STEP,
        "mmr": current,
        "days": days,
        "matches": recent,
        "shame": shame,
        "form": {"n": len(ranked := [m for m in all_rows if m["type"] == "Ranked"][:FORM]),
                 "w": sum(m["win"] for m in ranked)},
        "stats": period_stats(all_rows, items, days, const.get("regions", {}), heroes),
        "history": history_stats(load_history(db, heroes)),
    }

def load_history(db, heroes):
    by_name = {h["name"]: h for h in heroes.values()}
    by_name["Outworld Destroyer"] = by_name.get("Outworld Devourer") or by_name.get("Outworld Destroyer")
    rows = []
    for r in db.execute("select id, start, hero_id, hero_name, win, type, duration, k, d, a from matches order by start desc"):
        m = dict(r)
        hero = heroes.get(str(m["hero_id"])) or by_name.get(m["hero_name"]) or {}
        m["hero"], m["hero_img"] = hero.get("name") or m["hero_name"] or "?", hero.get("img")
        rows.append(m)
    return rows

def history_stats(rows):
    years = {}
    for m in rows:
        y = years.setdefault(m["start"][:4], {"year": m["start"][:4], "n": 0, "w": 0, "rn": 0, "rw": 0})
        y["n"] += 1
        y["w"] += m["win"]
        if m["type"] == "Ranked":
            y["rn"] += 1
            y["rw"] += m["win"]

    heroes = {}
    for m in rows:
        if m["hero_img"]:
            h = heroes.setdefault(m["hero"], {"hero": m["hero"], "img": m["hero_img"], "n": 0, "w": 0})
            h["n"] += 1
            h["w"] += m["win"]

    def record(key, f):
        with_val = [m for m in rows if f(m) is not None]
        m = max(with_val, key=f, default=None)
        return m and {"key": key, "value": f(m), "id": m["id"], "start": m["start"], "hero": m["hero"], "img": m["hero_img"], "win": bool(m["win"])}

    return {
        "n": len(rows),
        "w": sum(m["win"] for m in rows),
        "first": rows[-1]["start"] if rows else None,
        "years": sorted(years.values(), key=lambda y: y["year"], reverse=True),
        "heroes": sorted(heroes.values(), key=lambda h: (-h["n"], -h["w"]))[:HISTORY_TOP_HEROES],
        "streaks": streak_stats([m for m in rows if m["type"] == "Ranked"]),
        "records": [record("kills", lambda m: m["k"]), record("deaths", lambda m: m["d"]),
                    record("assists", lambda m: m["a"]), record("duration", lambda m: m["duration"])],
    }

def history_list(rows):
    names = sorted({m["hero"] for m in rows})
    idx = {n: i for i, n in enumerate(names)}
    imgs = {m["hero"]: m["hero_img"] for m in rows}
    return {
        "heroes": [[n, imgs[n]] for n in names],
        "matches": [[m["id"], m["start"][:16], idx[m["hero"]], m["win"], m["type"], m["duration"], m["k"], m["d"], m["a"]] for m in rows],
    }

def rating_series(rows, cal):
    chrono = sorted(rows, key=lambda m: m["start"])
    by_day = {}
    for m in chrono:
        m["day"] = m["start"][:10]
        m["delta"] = (STEP if m["win"] else -STEP) if m["type"] == "Ranked" else 0
        w, l, n = by_day.get(m["day"], (0, 0, 0))
        by_day[m["day"]] = (w + (m["delta"] > 0), l + (m["delta"] < 0), n + 1)

    extra = {}
    for d, (cw, cl) in cal.items():
        w, l, n = by_day.get(d, (0, 0, 0))
        if d >= BASE_DATE and cw + cl > n:
            extra[d] = (cw, cl, STEP * ((cw - w) - (cl - l)))

    mmr = BASE_MMR
    for m in reversed([m for m in chrono if m["start"] < BASE_TS]):
        m["mmr_after"] = mmr
        mmr -= m["delta"]
    start_mmr = mmr
    mmr = BASE_MMR
    for m in (m for m in chrono if m["start"] >= BASE_TS):
        mmr += m["delta"]
        m["mmr_after"] = mmr

    shift, shift_by_day = 0, {}
    for d in sorted(set(by_day) | set(extra)):
        shift += extra[d][2] if d in extra else 0
        shift_by_day[d] = shift
    for m in chrono:
        if m["start"] >= BASE_TS:
            m["mmr_after"] += shift_by_day[m["day"]]

    last_of_day = {m["day"]: m["mmr_after"] for m in chrono}
    points, prev = [], start_mmr
    for d in sorted(set(by_day) | set(extra)):
        end = last_of_day.get(d, prev + (extra[d][2] if d in extra else 0))
        w, l, _ = by_day.get(d, (0, 0, 0))
        if d in extra:
            w, l = extra[d][0], extra[d][1]
        points.append({"date": d, "w": w, "l": l, "mmr": end})
        prev = end
    current = points[-1]["mmr"] if points else BASE_MMR
    return {"start": start_mmr, "points": points}, current

REGION_GROUP = {
    1: "na", 2: "na", 27: "na", 31: "na",
    3: "eu", 8: "eu", 9: "eu", 28: "eu",
    10: "sa", 14: "sa", 15: "sa", 38: "sa",
    5: "asia", 16: "asia", 19: "asia", 24: "asia", 37: "asia",
    12: "cn", 13: "cn", 17: "cn", 18: "cn", 20: "cn", 25: "cn",
    6: "me", 7: "oce", 11: "af",
}
NEW_REGIONS = {"24": "Hong Kong", "27": "US North Central", "28": "Warsaw", "31": "US South Central"}
NEW_CLUSTERS = {129: 2, 189: 28, 190: 28, 436: 28, 404: 31, 405: 31, 406: 31, 407: 31, 410: 2}

def region_stats(rows, regions):
    by = {}
    for m in rows:
        if m["region"] is None:
            continue
        rid = m["region"] if m["region"] >= 0 else NEW_CLUSTERS.get(m["cluster"])
        group = REGION_GROUP.get(rid)
        name = (regions.get(str(rid)) or NEW_REGIONS.get(str(rid))) if group else None
        g = by.setdefault(group, {"group": group, "n": 0, "w": 0, "parts": {}})
        r = g["parts"].setdefault(name, {"region": name, "n": 0, "w": 0, "clusters": set()})
        for x in (g, r):
            x["n"] += 1
            x["w"] += m["win"]
        if m["region"] < 0 and m["cluster"] is not None:
            r["clusters"].add(m["cluster"])
    out = sorted(by.values(), key=lambda g: (g["group"] is None, -g["n"]))
    for g in out:
        g["parts"] = sorted(g["parts"].values(), key=lambda r: -r["n"])
        for r in g["parts"]:
            r["clusters"] = sorted(r["clusters"])
    return out

def matchup_stats(rows, heroes, side):
    by = {}
    for m in rows:
        for hid in m[side] or []:
            hero = heroes.get(str(hid))
            if not hero:
                continue
            h = by.setdefault(hid, {"hero": hero["name"], "img": hero["img"], "n": 0, "w": 0})
            h["n"] += 1
            h["w"] += m["win"]
    return sorted(by.values(), key=lambda h: -h["n"])

def draft_data(rows, heroes):
    return {
        "heroes": {hid: [h["name"], h["img"]] for hid, h in heroes.items()},
        "games": [[m["hero_id"], m["win"], m["allies"], m["enemies"]] for m in rows if m["enemies"] is not None],
    }

def period_stats(rows, items, days, regions, hero_const):
    rows = [m for m in rows if m["type"] == "Ranked"]
    days = days["points"]
    detailed = [m for m in rows if m["items"] is not None]
    cal_games = sum(d["w"] + d["l"] for d in days)
    wr = lambda ms: {"n": len(ms), "w": sum(m["win"] for m in ms)}

    by_hero = {}
    for m in rows:
        h = by_hero.setdefault(m["hero"], {"hero": m["hero"], "img": m["hero_img"], "n": 0, "w": 0, "k": 0, "d": 0, "a": 0})
        h["n"] += 1
        h["w"] += m["win"]
        h["k"] += m["k"] or 0
        h["d"] += m["d"] or 0
        h["a"] += m["a"] or 0
    heroes = sorted(by_hero.values(), key=lambda h: (-h["n"], -h["w"]))

    def has(m, iid):
        if m["purchased"] and items.get(str(iid)):
            return items[str(iid)]["key"] in m["purchased"]
        return iid in (m["items"] or []) or iid in (m["backpack"] or [])

    def radiance_block(ms):
        yes = [m for m in ms if has(m, RADIANCE)]
        no = [m for m in ms if not has(m, RADIANCE)]
        return {"with": wr(yes), "without": wr(no),
                "strip": [{"win": bool(m["win"]), "rad": has(m, RADIANCE)} for m in reversed(ms)]}

    spectre = [m for m in detailed if m["hero"] == "Spectre"]

    counts = {}
    for m in detailed:
        for iid in set((m["items"] or []) + (m["backpack"] or [])):
            it = items.get(str(iid))
            if iid and it and it["key"] not in SKIP_ITEMS and not it["key"].startswith("recipe"):
                counts[iid] = counts.get(iid, 0) + 1
    top = sorted(counts, key=lambda i: -counts[i])[:10]
    top_items = []
    for iid in top:
        inv = lambda m: iid in (m["items"] or []) or iid in (m["backpack"] or [])
        top_items.append({**item_ref(items, iid), "with": wr([m for m in detailed if inv(m)]),
                          "without": wr([m for m in detailed if not inv(m)])})

    return {
        "since": HISTORY_FROM,
        "games": len(rows),
        "detailed": len(detailed),
        "calendar_games": cal_games,
        "first": rows[-1]["start"] if rows else None,
        "overall": wr(rows),
        "heroes": heroes,
        "radiance": {"all": radiance_block(detailed), "spectre": radiance_block(spectre)},
        "items": top_items,
        "farm": farm_stats(detailed),
        "weeks": week_stats(detailed),
        "streaks": streak_stats(rows),
        "regions": region_stats(rows, regions),
        "lineups": sum(m["enemies"] is not None for m in rows),
        "vs": matchup_stats(rows, hero_const, "enemies"),
        "with": matchup_stats(rows, hero_const, "allies"),
        "draft": draft_data(rows, hero_const),
    }

def avg(ms, f):
    vals = [f(m) for m in ms]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None

FARM = {
    "gpm": lambda m: m["gpm"],
    "xpm": lambda m: m["xpm"],
    "lhm": lambda m: m["lh"] / (m["duration"] / 60) if m["lh"] is not None and m["duration"] else None,
    "nw": lambda m: m["net_worth"],
    "kda": lambda m: (m["k"] + m["a"]) / max(1, m["d"]),
    "hdm": lambda m: m["hero_damage"] / (m["duration"] / 60) if m["hero_damage"] is not None and m["duration"] else None,
    "dur": lambda m: m["duration"],
}

def farm_stats(detailed):
    now = datetime.now(timezone.utc)
    def window(lo, hi):
        return [m for m in detailed if lo <= (now - datetime.fromisoformat(m["start"])).total_seconds() / 86400 < hi]
    blocks = {"all": detailed, "week": window(0, 7), "prev": window(7, 14)}
    return {k: {"n": len(ms), **{f: avg(ms, fn) for f, fn in FARM.items()}} for k, ms in blocks.items()}

def week_stats(detailed):
    weeks = {}
    for m in detailed:
        d = datetime.fromisoformat(m["start"])
        key = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        weeks.setdefault(key, []).append(m)
    return [{"week": k, "n": len(ms), "w": sum(m["win"] for m in ms),
             "gpm": avg(ms, FARM["gpm"]), "xpm": avg(ms, FARM["xpm"]), "lhm": avg(ms, FARM["lhm"])}
            for k, ms in sorted(weeks.items())]

def streak_stats(rows):
    chrono = sorted(rows, key=lambda m: m["start"])
    best = {1: None, 0: None}
    i = 0
    while i < len(chrono):
        j = i
        while j + 1 < len(chrono) and chrono[j + 1]["win"] == chrono[i]["win"]:
            j += 1
        n, w = j - i + 1, chrono[i]["win"]
        if not best[w] or n > best[w]["n"]:
            best[w] = {"n": n, "from": chrono[i]["start"], "to": chrono[j]["start"]}
        i = j + 1
    cur = {"n": 0, "win": None}
    for m in reversed(chrono):
        if cur["win"] is None:
            cur["win"] = bool(m["win"])
        if bool(m["win"]) != cur["win"]:
            break
        cur["n"] += 1
    return {"win": best[1], "loss": best[0], "current": cur}

def item_ref(items, iid):
    if not iid:
        return None
    it = items.get(str(iid))
    return {"id": iid, "name": it["name"], "img": it["img"]} if it else None

def region_name(raw):
    name = raw.title().replace("Southafrica", "South Africa")
    return re.sub(r"\b(Us|Pw)\b", lambda m: m.group(1).upper(), name)

def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

def progress(i, total, label="", width=30):
    """Progress bar: in-place in a TTY, milestone lines in CI logs."""
    if total <= 0:
        return
    pct = i / total
    filled = int(width * pct)
    bar = "=" * filled + "-" * (width - filled)
    line = f"{label} [{bar}] {i}/{total} ({pct * 100:3.0f}%)"
    if sys.stdout.isatty():
        sys.stdout.write("\r" + line + ("" if i < total else "\n"))
        sys.stdout.flush()
    else:
        step = max(1, total // 20)  # ~20 lines max in CI
        if i == 1 or i == total or i % step == 0:
            print(line, flush=True)

# --- ENFORCE 3-HOUR SLEEP ---
if __name__ == "__main__":
    print("Starting continuous tracker...")
    while True:
        try:
            main(int(sys.argv[1]) if len(sys.argv) > 1 else OD_PER_RUN)
        except Exception as e:
            print(f"::error::Main loop error: {e}", file=sys.stderr)

        print(f"Sleeping for 3 hours (Next update at {datetime.now() + timedelta(hours=3)})...")
        time.sleep(SLEEP_SECONDS)

