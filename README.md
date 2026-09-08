# cbs-pickem

Reads and saves data from a CBS Pick Em Contest.

- uv run playwright install

## Configuration

- `config/config.py`'s `SEASON` constant has to be bumped by hand each year. CBS's pool-home page does have a `season.year` field, and Sports IO has `/leagues?current=true` until I add support for that once a year bump is needed. Both CBS and Sports IO loaders warn if the source's season doesn't match `SEASON`
