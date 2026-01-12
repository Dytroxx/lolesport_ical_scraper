from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True, slots=True)
class Match:
    league_slug: str
    league_name: str
    match_id: Optional[str]  # Stable match identifier when available
    match_start_utc: datetime
    match_start_local: datetime
    best_of: Optional[str]
    team1: str
    team2: str
    team1_code: Optional[str]  # Short code like "FNC", "G2"
    team2_code: Optional[str]  # Short code like "T1", "GEN"
    stage: Optional[str]
    match_url: str
    stable_uid: str
    # Match result fields (populated for completed matches)
    state: Optional[str] = None  # "unstarted", "inProgress", "completed"
    team1_score: Optional[int] = None
    team2_score: Optional[int] = None
    winner: Optional[str] = None  # team name of winner
