from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True, slots=True)
class Match:
    league_slug: str
    league_name: str
    match_start_utc: datetime
    match_start_local: datetime
    best_of: Optional[str]
    team1: str
    team2: str
    stage: Optional[str]
    match_url: str
    stable_uid: str
