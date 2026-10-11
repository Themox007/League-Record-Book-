#!/usr/bin/env python3
"""Pulls every season of an ESPN fantasy football league into data.json:
team scores for every matchup, plus every rostered player's score each week.

Needs two environment variables for a private league:
  ESPN_S2  - the espn_s2 cookie from your logged-in browser
  SWID     - the SWID cookie (looks like {XXXXXXXX-XXXX-...})
Uses only Python's standard library, so there is nothing to install.
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

LEAGUE_ID = os.environ.get("LEAGUE_ID", "2004281526")
ESPN_S2 = os.environ.get("ESPN_S2", "").strip()
SWID = os.environ.get("SWID", "").strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

BASE = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
        "seasons/{year}/segments/0/leagues/{league}")
VIEWS = ["mTeam", "mMatchupScore", "mSettings", "mStatus", "mStandings"]
PLAYER_VIEWS = ["mMatchupScore", "mScoreboard", "mMatchup", "mBoxscore"]
POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}


class SeasonMissing(Exception):
    pass


class Problem(Exception):
    pass


def fetch(year, views=VIEWS, week=None):
    url = BASE.format(year=year, league=LEAGUE_ID) + "?" + "&".join("view=" + v for v in views)
    if week:
        url += "&scoringPeriodId=%d" % week
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (league-record-book)"}
    if ESPN_S2 and SWID:
        swid = SWID if SWID.startswith("{") else "{" + SWID.strip("{}") + "}"
        headers["Cookie"] = "espn_s2=%s; SWID=%s" % (ESPN_S2, swid)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Problem("ESPN refused access for %s (error %s). The league is private, so the "
                          "ESPN_S2 and SWID cookies are missing, wrong, or expired. Copy fresh "
                          "ones from your browser and update the two GitHub secrets." % (year, e.code))
        if e.code == 404:
            raise SeasonMissing(year)
        raise Problem("ESPN returned error %s for %s." % (e.code, year))
    except urllib.error.URLError as e:
        raise Problem("Couldn't reach ESPN: %s" % e.reason)
    try:
        data = json.loads(body)
    except ValueError:
        raise Problem("ESPN sent back a web page instead of data for %s. This usually means "
                      "the cookies are missing or expired." % year)
    if isinstance(data, list):  # older "leagueHistory" style responses come wrapped in a list
        if not data:
            raise SeasonMissing(year)
        data = data[0]
    return data


def clean(text):
    return " ".join((text or "").split())


def full_name(member):
    return clean("%s %s" % (member.get("firstName") or "", member.get("lastName") or "")).lower()


def short_name(member):
    first = clean(member.get("firstName")).title()
    last = clean(member.get("lastName")).title()
    if first and last:
        return "%s %s." % (first, last[0])
    return first or clean(member.get("displayName")) or "Unknown manager"


def side(s):
    if not s or s.get("teamId") is None:
        return None
    return {"teamId": s["teamId"], "pts": round(float(s.get("totalPoints") or 0), 2)}


def parse_season(d, year, manager_ids, managers, owner_names=None):
    owner_names = {} if owner_names is None else owner_names
    members = {m["id"]: m for m in d.get("members", [])}
    owner_of = {}
    for t in d.get("teams", []):
        owner_of[t["id"]] = t.get("primaryOwner") or (t.get("owners") or [None])[0] or "team-%s" % t["id"]
    this_season = set(owner_of.values())
    teams = []
    for t in d.get("teams", []):
        owner = owner_of[t["id"]]
        name = full_name(members[owner]) if owner in members else ""
        # ESPN's member id is half of that person's login cookie, so it never goes
        # into data.json. Each manager gets a neutral id (m1, m2, ...) instead.
        if owner not in manager_ids:
            # Someone who came back under a new ESPN account: same name as an earlier
            # manager who isn't in this season. Treat them as the same person.
            twin = next((o for o, n in owner_names.items()
                         if name and n == name and o not in this_season), None)
            manager_ids[owner] = manager_ids[twin] if twin else "m%d" % (len(set(manager_ids.values())) + 1)
        if name:
            owner_names[owner] = name
        mid = manager_ids[owner]
        if owner in members:
            managers[mid] = {"name": short_name(members[owner])}
        managers.setdefault(mid, {"name": "Former manager %s" % mid[1:]})
        team_name = clean(t.get("name")) or clean("%s %s" % (t.get("location", ""), t.get("nickname", "")))
        rec = (t.get("record") or {}).get("overall") or {}
        teams.append({
            "teamId": t["id"],
            "name": team_name or t.get("abbrev") or "Team %s" % t["id"],
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
    }, clean(settings.get("name"))


def week_points(player, week):
    for st in player.get("stats") or []:
        if (st.get("scoringPeriodId") == week and st.get("statSourceId") == 0
                and st.get("statSplitTypeId") == 1 and st.get("appliedTotal") is not None):
            return st["appliedTotal"]
    return None


def parse_player_week(d, week, players, seen):
    """One week's response -> rows of [week, teamId, playerId, lineup, points].
    lineup: 1 = started, 0 = bench, 2 = injured reserve."""
    rows = []
    for m in d.get("schedule", []):
        if (m.get("winner") or "UNDECIDED") == "UNDECIDED":
            continue  # week not finished yet
        for key in ("home", "away"):
            s = m.get(key) or {}
            roster = s.get("rosterForCurrentScoringPeriod")
            if not roster or s.get("teamId") is None:
                continue
            for e in roster.get("entries") or []:
                pool = e.get("playerPoolEntry") or {}
                p = pool.get("player") or {}
                pid = p.get("id", e.get("playerId"))
                if pid is None or not p.get("fullName") or (week, s["teamId"], pid) in seen:
                    continue
                slot = e.get("lineupSlotId")
                lineup = 0 if slot == 20 else 2 if slot == 21 else 1
                pts = week_points(p, week)
                if pts is None:
                    pts = pool.get("appliedStatTotal")
                if pts is None:
                    if lineup != 1:
                        continue  # bench player on a bye: nothing to record
                    pts = 0
                seen.add((week, s["teamId"], pid))
                players[str(pid)] = [clean(p["fullName"]), POSITIONS.get(p.get("defaultPositionId"), "")]
                rows.append([week, s["teamId"], pid, lineup, round(float(pts), 2)])
    return rows


def fetch_players(year, last_week, players):
    rows, seen = [], set()
    for week in range(1, last_week + 1):
        rows += parse_player_week(fetch(year, PLAYER_VIEWS, week), week, players, seen)
        time.sleep(0.3)
    return rows


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

    manager_ids, managers, owner_names, seasons, league_name = {}, {}, {}, [], None
    for y in sorted(raw):  # oldest first keeps manager ids stable from run to run
        season, name = parse_season(raw[y], y, manager_ids, managers, owner_names)
        league_name = name or league_name
        seasons.append(season)
        print("  %s: %d teams, %d matchups" % (y, len(season["teams"]), len(season["games"])))

    if not any(s["games"] for s in seasons):
        sys.exit("ESPN returned no matchups at all, so data.json was left untouched.")

    # Player scores. Finished seasons never change, so they are fetched once and
    # reused from the existing data.json on later runs.
    old = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        pass
    if old.get("demo"):
        old = {}
    players = dict(old.get("players") or {})
    old_seasons = {s["year"]: s for s in old.get("seasons", [])}
    for season in seasons:
        y = season["year"]
        prior = old_seasons.get(y, {})
        finished = any(t["finalRank"] for t in season["teams"])
        if prior.get("playersDone") and prior.get("playerWeeks"):
            season["playerWeeks"], season["playersDone"] = prior["playerWeeks"], True
            print("  %s players: reused %d saved scores" % (y, len(prior["playerWeeks"])))
            continue
        status = raw[y].get("status") or {}
        last = status.get("finalScoringPeriod") or 18
        if not finished:
            last = min(last, status.get("latestScoringPeriod") or season["currentWeek"] or last)
        try:
            season["playerWeeks"] = fetch_players(y, last, players)
            season["playersDone"] = finished and bool(season["playerWeeks"])
            print("  %s players: %d scores across weeks 1-%d" % (y, len(season["playerWeeks"]), last))
        except (Problem, SeasonMissing) as e:
            season["playerWeeks"] = prior.get("playerWeeks") or []
            print("  %s players: FAILED, kept what was there before (%s)" % (y, e))

    out = {
        "demo": False,
        "league": {"name": league_name or "Fantasy League"},
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "managers": managers,
        "players": players,
        "seasons": seasons,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print("Saved %d seasons to data.json" % len(seasons))


if __name__ == "__main__":
    try:
        main()
    except Problem as e:
        sys.exit(str(e))
