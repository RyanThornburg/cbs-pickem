# cbs-pickem

Reads and saves data from a CBS Pick Em Contest.

- uv run playwright install

## Configuration

- `config/config.py`'s `SEASON` constant has to be bumped by hand each year. CBS's pool-home page does have a `season.year` field, and Sports IO has `/leagues?current=true` until I add support for that once a year bump is needed. Both CBS and Sports IO loaders warn if the source's season doesn't match `SEASON`

## End of season

Before bumping `SEASON` for the next year, close out the season that just
finished: `uv run python -m src.season_close_out [local|prod]`. This
writes that season's final standings into `historical_standings` (used
for the historical winners/standings page) and refreshes the historical KV
key. Only run it once the season's actual final week has been played —
see `CLAUDE.md`'s "End of season" section for why running it early would
silently record a partial-season score as final.
