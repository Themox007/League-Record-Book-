#!/usr/bin/env python3
"""Pulls every season of an ESPN fantasy football league into data.json.

Needs two environment variables for a private league:
  ESPN_S2  - the espn_s2 cookie from your logged-in browser
  SWID     - the SWID cookie (looks like {XXXXXXXX-XXXX-...})
Uses only Python's standard library, so there is nothing to install.
"""
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

LEAGUE_ID = os.environ.get("LEAGUE_ID", "2004281526")
ESPN_S2 = os.environ.get("ESPN_S2", "").strip()
SWID = os.environ.get("SWID", "").strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

BASE = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
        "seasons/{year}/segments/0/leagues/{league}")
VIEWS = ["mTeam", "mMatchupScore", "mSettings", "mStatus", "mStandings"]


class SeasonMissing(Exception):
    pass


def fetch(year):
    url = BASE.format(year=year, league=LEAGUE_ID) + "?" + "&".join("view=" + v for v in VIEWS)
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (league-record-book)"}
    if ESPN_S2 and SWID:
        swid = SWID if SWID.startswith("{") else "{" + SWID.strip("{}") + "}"
        headers["Cookie"] = "espn_s2=%s; SWID=%s" % (ESPN_S2, swid)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("ESPN refused access for %s (error %s). The league is private, so the "
                     "ESPN_S2 and SWID cookies are missing, wrong, or expired. Copy fresh "
                     "ones from your browser and update the two GitHub secrets." % (year, e.code))
        if e.code == 404:
            raise SeasonMissing(year)
        raise
    try:
        data = json.loads(body)
    except ValueError:
        sys.exit("ESPN sent back a web page instead of data for %s. This usually means "
                 "the cookies are missing or expired." % year)
    if isinstance(data, list):  # older "leagueHistory" style responses come wrapped in a list
        if not data:
            raise SeasonMissing(year)
        data = data[0]
    return data


def short_name(member):
    first = (member.get("firstName") or "").strip().title()
    last = (member.get("lastName") or "").strip().title()
    if first and last:
        return "%s %s." % (first, last[0])
    return first or (member.get("displayName") or "Unknown manager")


def side(s):
    if not s or s.get("teamId") is None:
        return None
    return {"teamId": s["teamId"], "pts": round(float(s.get("totalPoints") or 0), 2)}


def parse_season(d, year, manager_ids, managers):
    members = {m["id"]: m for m in d.get("members", [])}
    teams = []
    for t in d.get("teams", []):
        owner = t.get("primaryOwner") or (t.get("owners") or [None])[0] or "team-%s" % t["id"]
        # ESPN's member id is half of that person's login cookie, so it never goes
        # into data.json. Each manager gets a neutral id (m1, m2, ...) instead.
        if owner not in manager_ids:
            manager_ids[owner] = "m%d" % (len(manager_ids) + 1)
        mid = manager_ids[owner]
        if owner in members:
            managers[mid] = {"name": short_name(members[owner])}
        managers.setdefault(mid, {"name": "Former manager %s" % mid[1:]})
        name = t.get("name") or ("%s %s" % (t.get("location", ""), t.get("nickname", ""))).strip()
        rec = (t.get("record") or {}).get("overall") or {}
        teams.append({
            "teamId": t["id"],
            "name": name or t.get("abbrev") or "Team %s" % t["id"],
            "managerId": mid,
            "wins": rec.get("wins", 0), "losses": rec.get("losses", 0), "ties": rec.get("ties", 0),
            "pf": round(float(rec.get("pointsFor") or 0), 2),
            "pa": round(float(rec.get("pointsAgainst") or 0), 2),
            "finalRank": t.get("rankCalculatedFinal") or 0,
        })
    games = []
    for m in d.get("schedule", []):
        home, away = side(m.get("home")), side(m.get("away"))
        if not home and not away:
            continue
        games.append({
            "week": m.get("matchupPeriodId"),
            "tier": m.get("playoffTierType") or "NONE",
            "home": home, "away": away,
            "winner": m.get("winner") or "UNDECIDED",
        })
    settings = d.get("settings") or {}
    return {
        "year": year,
        "regularWeeks": (settings.get("scheduleSettings") or {}).get("matchupPeriodCount"),
        "currentWeek": (d.get("status") or {}).get("currentMatchupPeriod"),
        "teams": teams,
        "games": games,
    }, settings.get("name")


def main():
    today = datetime.date.today()
    year = today.year if today.month >= 7 else today.year - 1
    try:
        current = fetch(year)
    except SeasonMissing:
        year -= 1
        try:
            current = fetch(year)
        except SeasonMissing:
            sys.exit("Couldn't find league %s on ESPN. Check the league ID." % LEAGUE_ID)

    raw = {year: current}
    previous = (current.get("status") or {}).get("previousSeasons")
    if previous:
        for y in previous:
            try:
                raw[y] = fetch(y)
            except SeasonMissing:
                print("  %s: not available, skipped" % y)
    else:  # ESPN didn't list past seasons, so walk backwards until one is missing
        for y in range(year - 1, year - 20, -1):
            try:
                raw[y] = fetch(y)
            except SeasonMissing:
                break

    manager_ids, managers, seasons, league_name = {}, {}, [], None
    for y in sorted(raw):  # oldest first keeps manager ids stable from run to run
        season, name = parse_season(raw[y], y, manager_ids, managers)
        league_name = name or league_name
        seasons.append(season)
        print("  %s: %d teams, %d matchups" % (y, len(season["teams"]), len(season["games"])))

    if not any(s["games"] for s in seasons):
        sys.exit("ESPN returned no matchups at all, so data.json was left untouched.")

    out = {
        "demo": False,
        "league": {"name": league_name or "Fantasy League"},
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "managers": managers,
        "seasons": seasons,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print("Saved %d seasons to data.json" % len(seasons))


if __name__ == "__main__":
    main()
