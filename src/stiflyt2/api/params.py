"""Query parameters that many endpoints take alike."""
from __future__ import annotations

from typing import Annotated

from fastapi import Query

from stiflyt2 import seasons

# The season asked for: the summer's routes unless the winter's (DNT's winter routes) are.
Season = Annotated[seasons.Season, Query(description="summer, or winter: DNT's winter routes")]
