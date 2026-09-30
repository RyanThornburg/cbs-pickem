"""The one D1Client/KVClient per process, built from config on first use.

Every loader/kv_writer module goes through these rather than constructing
its own client - one requests.Session (and its kept-alive connection) for
the whole run, and a single place for tests to swap in fakes
(tests/conftest.py's `clients` fixture). load_env() must have run first,
same as before, since the config is only read on the first call.
"""

from functools import cache

from config.config import get_d1_config, get_kv_config
from db.d1_client import D1Client
from db.kv_client import KVClient


@cache
def get_d1() -> D1Client:
    return D1Client(**get_d1_config())


@cache
def get_kv() -> KVClient:
    return KVClient(**get_kv_config())
