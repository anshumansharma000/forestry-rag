"""Distributed web-search admission and conservative gross-cost reservations.

These cap new admissions, not queries executed inside an in-flight Gemini call.
Redis outages deny search; there is deliberately no per-process fallback.
"""

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from errors import AppError, ErrorCode
from generation_cost import generation_cost, positive_setting, search_cost
from pipeline_trace import event
from request_limits import client
from settings import env_int

RESERVE = """
if redis.call('EXISTS',KEYS[5])==1 then return 0 end
local increments={1,1,tonumber(ARGV[5]),tonumber(ARGV[6])}
for i=1,4 do
  if tonumber(redis.call('GET',KEYS[i]) or '0')+increments[i]>tonumber(ARGV[i]) then return 0 end
end
for i=1,4 do
  redis.call('INCRBY',KEYS[i],increments[i]); redis.call('EXPIRE',KEYS[i],3456000)
end
redis.call('SET',KEYS[6],'pending','EX',3456000)
return 1
"""
SETTLE = """
if redis.call('GET',KEYS[6])~='pending' then return 0 end
redis.call('INCRBY',KEYS[3],tonumber(ARGV[1])-tonumber(ARGV[3]))
redis.call('INCRBY',KEYS[4],tonumber(ARGV[2])-tonumber(ARGV[4]))
if ARGV[5]=='unknown' then redis.call('SET',KEYS[5],'unknown-cost','EX',3456000) end
redis.call('SET',KEYS[6],'settled','EX',3456000)
return 1
"""


@dataclass
class WebReservation:
    connection: object
    keys: list[str]
    queries: int
    micros: int

    def settle(self, result=None):
        queries, micros, status = self.queries, self.micros, "unknown"
        if result is not None:
            search = search_cost(result.model, result.grounding_metadata)
            tokens = generation_cost(result.model, result.usage_metadata)
            if search["estimated_cost_usd"] is not None and tokens["estimated_cost_usd"] is not None:
                queries = search["search_queries"]
                micros = math.ceil((search["estimated_cost_usd"] + tokens["estimated_cost_usd"]) * 1_000_000)
                status = "known"
        try:
            self.connection.eval(SETTLE, len(self.keys), *self.keys, queries, micros, self.queries, self.micros, status)
            event("web_budget_settled", search_queries=queries, cost_microusd=micros, status=status)
        except Exception:
            # Reservation remains charged. Deny future admissions through Redis
            # outage; if Redis recovers, try to latch the uncertainty circuit.
            try:
                self.connection.set(self.keys[4], "settlement-failed", ex=3456000)
            except Exception:
                pass
            event("web_budget_settlement_failed")


def reserve_web_search(user_id: str | None) -> WebReservation | None:
    if not user_id:
        event("web_search_denied", reason="missing_identity")
        return None
    # Budget is gross USD, independent of exchange rates and Google's shared free allowance.
    daily_usd = positive_setting("RAG_WEB_DAILY_BUDGET_USD", 5.0)
    reserve_usd = positive_setting("RAG_WEB_RESERVE_USD", .10)
    if daily_usd is None or reserve_usd is None:
        raise AppError("Invalid web search budget configuration.", code=ErrorCode.CONFIG_ERROR)
    queries = env_int("RAG_WEB_RESERVED_QUERIES", 3)
    micros = math.ceil(reserve_usd * 1_000_000)
    try:
        connection = client()
        # Use the shared server's clock for consistent windows across replicas.
        seconds, _ = connection.time()
        now = datetime.fromtimestamp(int(seconds), UTC)
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        identity = hashlib.sha256(str(user_id).encode()).hexdigest()
        prefix = "{rag-web}:"
        keys = [prefix + day + ":user:" + identity, prefix + day + ":calls",
                prefix + month + ":queries", prefix + day + ":microusd", prefix + day + ":blocked",
                prefix + "reservation:" + str(uuid4())]
        allowed = connection.eval(
            RESERVE, len(keys), *keys,
            env_int("RAG_WEB_USER_DAILY_CALLS", 20), env_int("RAG_WEB_GLOBAL_DAILY_CALLS", 200),
            env_int("RAG_WEB_MONTHLY_QUERIES", 4000), math.floor(daily_usd * 1_000_000), queries, micros,
        )
    except AppError:
        raise
    except Exception:
        event("web_search_denied", reason="quota_store_unavailable")
        return None
    if not allowed:
        event("web_search_denied", reason="quota_or_budget")
        return None
    return WebReservation(connection, keys, queries, micros)
