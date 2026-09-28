"""
Team code / nickname lookup tables for matching games across VegasInsider
(DK+FD source, uses nicknames like 'Orioles') and Kalshi (uses short codes
like 'BAL' embedded in event tickers, plus city names in titles).
"""

MLB_CODES = {
    'ARI': 'Diamondbacks', 'AZ': 'Diamondbacks', 'ATL': 'Braves', 'BAL': 'Orioles',
    'BOS': 'Red Sox', 'CHC': 'Cubs', 'CWS': 'White Sox', 'CIN': 'Reds', 'CLE': 'Guardians',
    'COL': 'Rockies', 'DET': 'Tigers', 'HOU': 'Astros', 'KC': 'Royals', 'LAA': 'Angels',
    'LAD': 'Dodgers', 'MIA': 'Marlins', 'MIL': 'Brewers', 'MIN': 'Twins', 'NYM': 'Mets',
    'NYY': 'Yankees', 'OAK': 'Athletics', 'ATH': 'Athletics', 'PHI': 'Phillies',
    'PIT': 'Pirates', 'SD': 'Padres', 'SF': 'Giants', 'SEA': 'Mariners', 'STL': 'Cardinals',
    'TB': 'Rays', 'TEX': 'Rangers', 'TOR': 'Blue Jays', 'WSH': 'Nationals', 'WAS': 'Nationals',
}

NFL_CODES = {
    'ARI': 'Cardinals', 'ATL': 'Falcons', 'BAL': 'Ravens', 'BUF': 'Bills', 'CAR': 'Panthers',
    'CHI': 'Bears', 'CIN': 'Bengals', 'CLE': 'Browns', 'DAL': 'Cowboys', 'DEN': 'Broncos',
    'DET': 'Lions', 'GB': 'Packers', 'HOU': 'Texans', 'IND': 'Colts', 'JAX': 'Jaguars',
    'KC': 'Chiefs', 'LAC': 'Chargers', 'LAR': 'Rams', 'LV': 'Raiders', 'MIA': 'Dolphins',
    'MIN': 'Vikings', 'NE': 'Patriots', 'NO': 'Saints', 'NYG': 'Giants', 'NYJ': 'Jets',
    'PHI': 'Eagles', 'PIT': 'Steelers', 'SEA': 'Seahawks', 'SF': 'F49ers', 'TB': 'Buccaneers',
    'TEN': 'Titans', 'WAS': 'Commanders', 'WSH': 'Commanders',
}
# NFL 49ers nickname fix (can't start a python-safe key with digit in comments; stored plainly)
NFL_CODES['SF'] = '49ers'

CODE_TABLES = {'MLB': MLB_CODES, 'NFL': NFL_CODES}


def split_team_codes(blob, code_table):
    """Given a concatenated code blob like 'SEALAA' or 'MIAAZ', split into
    (code1, code2) using the known code table. Tries all valid combinations
    of prefix lengths (2-5 chars) for the first code -- NCAAF has codes up to
    5 chars (e.g. 'UTRGV' = UT Rio Grande Valley); MLB/NFL codes are all <=3
    chars so this wider range is a no-op for them (still requires an exact
    match in code_table for both halves, so no new false-positive risk)."""
    for n in (2, 3, 4, 5):
        c1 = blob[:n]
        c2 = blob[n:]
        if c1 in code_table and c2 in code_table:
            return c1, c2
    return None, None


def nickname_for_code(code, code_table):
    return code_table.get(code)
