"""Redis Job Adapter.

Every move a job makes is one Lua script, so it is one atomic step on the
Redis server, timed by the server's own clock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from typing_extensions import Doc

from grelmicro._duration import MICROSECOND, MILLISECOND, round_up
from grelmicro.providers.redis import RedisProvider, kind_prefix
from grelmicro.task._protocol import (
    FIRST,
    JOB_LOST,
    JOB_NOT_STARTED,
    JOB_TIMED_OUT,
    Beat,
    BeatReply,
    Claimed,
    EndStatus,
    JobRecord,
    JobStatus,
    Listed,
    ProgressUpdate,
    Settled,
    Submitted,
    page_of,
    read_cursor,
    scan_limit,
)

if TYPE_CHECKING:
    import builtins
    from collections.abc import Mapping, Sequence
    from types import TracebackType

    from grelmicro.types import BackendScope

_LUA_JOB = (
    """
    local now_pair = redis.call('TIME')
    local now = tonumber(now_pair[1]) * 1000000 + tonumber(now_pair[2])
    local deadlines = KEYS[1]
    local base = string.sub(deadlines, 1, #deadlines - #'deadlines')
    local expiries = base .. 'expires'
    local JOB_LOST = """
    + json.dumps(JOB_LOST)
    + """
    local JOB_NOT_STARTED = """
    + json.dumps(JOB_NOT_STARTED)
    + """
    local JOB_TIMED_OUT = """
    + json.dumps(JOB_TIMED_OUT)
    + """

    local function us(value)
        return string.format('%.0f', value)
    end

    local function record(key)
        return redis.call(
            'HMGET', key,
            'n', 's', 'v', 'd', 't', 'r', 'e', 'c', 'ca', 'sa', 'fa'
        )
    end

    local function owns(stored, has_owner, owner)
        if has_owner == '1' then
            return stored == owner
        end
        return stored == false
    end

    local function finish(key, id, status, err, queue, waiting)
        redis.call('HSET', key, 's', status, 'fa', us(now))
        if err then
            redis.call('HSET', key, 'e', err)
        end
        redis.call('HDEL', key, 'p', 'x', 'lu', 'sb', 'mr', 'rb')
        redis.call('HINCRBY', key, 'v', 1)
        redis.call('ZREM', queue, id)
        redis.call('ZREM', waiting, id)
        redis.call('ZREM', deadlines, id)
    end

    local function owned_entry(own, id)
        return #id .. ':' .. id .. own
    end

    local function ends_of(kept)
        return base .. 'kept-ends:' .. string.sub(kept, #base + 6)
    end

    local function unindex(index, id)
        redis.call('ZREM', index, id)
        redis.call('ZREM', expiries, owned_entry(index, id))
        if string.sub(index, 1, #base + 5) == base .. 'kept:' then
            redis.call('ZREM', ends_of(index), id)
        end
    end

    local function forget(key, id)
        local job = redis.call('HMGET', key, 'ok', 'kk')
        for _, index in ipairs({job[1], job[2]}) do
            if index then
                unindex(index, id)
            end
        end
        redis.call('DEL', key)
    end

    local function trim(kept, cap)
        local due = redis.call(
            'ZRANGEBYSCORE', ends_of(kept), '-inf', us(math.floor(now / 1000))
        )
        for _, id in ipairs(due) do
            unindex(kept, id)
        end
        while redis.call('ZCARD', kept) > cap do
            local id = redis.call('ZRANGE', kept, 0, 0)[1]
            local key = base .. 'id:' .. id
            if redis.call('HGET', key, 'kk') == kept then
                forget(key, id)
            else
                unindex(kept, id)
            end
        end
    end

    local function retain(key, id)
        local job = redis.call('HMGET', key, 'k', 'ok', 's', 'n', 'ms', 'mf')
        local keep, own = job[1], job[2]
        local kind, cap = 'failed', job[6]
        if job[3] == 'succeeded' then
            kind, cap = 'succeeded', job[5]
        end
        local kept = false
        if cap then
            kept = base .. 'kept:' .. kind .. ':' .. job[4]
        end
        if keep == '0' or cap == '0' then
            forget(key, id)
            if kept then
                trim(kept, 0)
            end
            return
        end
        if kept then
            redis.call('HSET', key, 'kk', kept)
        end
        if keep then
            redis.call('PEXPIRE', key, keep)
            local ends = us(math.floor(now / 1000) + tonumber(keep) + 1)
            for _, index in ipairs({own or false, kept}) do
                if index then
                    redis.call('ZADD', expiries, ends, owned_entry(index, id))
                end
            end
            if kept then
                redis.call('ZADD', ends_of(kept), ends, id)
            end
        end
        if kept then
            local order = redis.call('INCR', base .. 'kept-order')
            redis.call('ZADD', kept, order, id)
            trim(kept, tonumber(cap))
        end
    end

    local function catch_up(key, id, queue, waiting)
        local job = redis.call('HMGET', key, 's', 'lu', 'sb', 'rb')
        local err = false
        if job[1] == 'running' then
            local ends_at, run_by = tonumber(job[2]), tonumber(job[4])
            err = JOB_LOST
            if run_by and run_by <= ends_at then
                ends_at, err = run_by, JOB_TIMED_OUT
            end
            if ends_at > now then
                return false
            end
            redis.call('HDEL', key, 'rn')
        elseif job[1] == 'queued' and job[3] and tonumber(job[3]) <= now then
            err = JOB_NOT_STARTED
        else
            return false
        end
        finish(key, id, 'failed', err, queue, waiting)
        retain(key, id)
        return err
    end

    local function hold(key, id, lease)
        local lease_until = now + lease
        redis.call('HSET', key, 'lu', us(lease_until))
        local run_by = tonumber(redis.call('HGET', key, 'rb'))
        if run_by and run_by < lease_until then
            lease_until = run_by
        end
        redis.call('ZADD', deadlines, us(lease_until), id)
    end

    local function start_running(key, id, runner, lease)
        redis.call('HSET', key, 's', 'running', 'rn', runner, 'sa', us(now))
        local max_run = redis.call('HGET', key, 'mr')
        if max_run then
            redis.call('HSET', key, 'rb', us(now + tonumber(max_run)))
        end
        hold(key, id, lease)
    end

    local function write_progress(key, done, total)
        redis.call('HSET', key, 'd', done)
        if total == '' then
            redis.call('HDEL', key, 't')
        else
            redis.call('HSET', key, 't', total)
        end
    end
"""
)
"""What every script shares: the server's time and the moves every call makes.

`KEYS[1]` is always the store's deadline index. It scores a running job by
the earlier of its lease end and its `max_run` end, and a queued one by its
`max_wait` end. `catch_up` returns the error it ended a job with, or `false`.

An owned job's `ok` field names its owner index. A job with a cap of its
kind of end names its kept index in its `kk` field: the ended jobs of its
name and kind, scored by end order. Its `kept-ends` twin scores the same
members by when each expires in ms, by its own `keep`. `trim` first drops
the members past their own `keep`, then, past the cap, forgets the oldest
member whose job still names the index, and drops a member whose job is gone.
A job that ends with a positive `keep` puts one entry per index it is in in
the store's `expires` index, scored by when the job expires in ms, so `purge`
takes the job out of each index then. `forget` removes a job with every
entry that names it. The store's key prefix is `KEYS[1]` without its
`deadlines` suffix.
"""


class RedisJobAdapter:
    """Redis job storage backend.

    Wraps a `RedisProvider` and implements the `JobBackend` protocol. Every
    call is one Lua script, so a job's move and the indexes it touches change
    in one atomic step. Leases, `max_wait` and the creation and end times
    follow the Redis server clock, and `keep` is a key expiry on the same
    clock, so `purge` has nothing left to do.

    Each job is one hash, keyed by its id. Each name has its queue, in
    submission order, and the queued jobs that have a `max_wait`. One index
    holds every deadline, so `reap` finds the overdue jobs of every name.
    A job with a cap on its kind of end joins a sorted set of the ended jobs
    of its name and kind, in end order, written only when that cap is set.
    The move that ends it forgets the oldest past the cap, in the same
    script, with every index entry that names them.

    On a Redis Cluster, every key of the store lands in one slot: under the
    hash tag a prefix carries, or under `{<prefix>job}:` when none does.

    Pass an explicit `provider=` to share a pool with other components, or
    rely on the default `env_prefix=` to build one from environment variables.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    _KEY_PREFIX = "job:"
    """Marks the keys of the job store when the prefix names no kind."""

    _LUA_SUBMIT = (
        _LUA_JOB
        + """
        local key, queue, waiting, order = KEYS[2], KEYS[3], KEYS[4], KEYS[5]
        local own = KEYS[6]
        local id, name, has_owner, owner = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
        local has_runner, runner = ARGV[10], ARGV[11]
        local stored = redis.call('HMGET', key, 'n', 'o')
        if stored[1] then
            if stored[1] ~= name or not owns(stored[2], has_owner, owner) then
                return false
            end
            catch_up(key, id, queue, waiting)
            local job = redis.call('HMGET', key, 's', 'rn', 'sas')
            if job[1] then
                local started = job[3] == '1' and job[1] == 'running'
                    and has_runner == '1' and job[2] == runner
                return {started and 1 or 0, record(key)}
            end
        end
        local live = redis.call('ZCARD', queue)
            - redis.call('ZCOUNT', waiting, '-inf', us(now))
        local start = has_runner == '1' and live == 0
        local max_queued = tonumber(ARGV[7])
        if not start and max_queued >= 0 and live >= max_queued then
            return false
        end
        redis.call(
            'HSET', key, 'n', name, 's', 'queued', 'v', 1, 'd', '0',
            'c', '0', 'ca', us(now), 'p', ARGV[5], 'x', ARGV[6]
        )
        local seq = redis.call('INCR', order)
        if has_owner == '1' then
            redis.call('HSET', key, 'o', owner, 'ok', own)
            redis.call('ZADD', own, seq, id)
        end
        if ARGV[9] ~= '' then
            redis.call('HSET', key, 'k', ARGV[9])
        end
        if ARGV[13] ~= '' then
            redis.call('HSET', key, 'mr', ARGV[13])
        end
        if ARGV[14] ~= '' then
            redis.call('HSET', key, 'ms', ARGV[14])
        end
        if ARGV[15] ~= '' then
            redis.call('HSET', key, 'mf', ARGV[15])
        end
        if start then
            redis.call('HSET', key, 'sas', '1')
            start_running(key, id, runner, tonumber(ARGV[12]))
            return {1, record(key)}
        end
        local max_wait = tonumber(ARGV[8])
        if max_wait >= 0 then
            local start_by = us(now + max_wait)
            redis.call('HSET', key, 'sb', start_by)
            redis.call('ZADD', waiting, start_by, id)
            redis.call('ZADD', deadlines, start_by, id)
        end
        redis.call('ZADD', queue, seq, id)
        return {0, record(key)}
    """
    )
    """Store a job, or refuse it.

    KEYS: deadlines, job, queue, waiting, order, then the owner index with
    an owner. ARGV: id, name, has owner,
    owner, payload, context, max queued (-1: no limit), max wait in us (-1: no
    limit), keep in ms ('': no limit), has runner, runner, lease in us, max
    run in us ('': no limit), max kept succeeded and max kept failed ('': no
    limit).
    """

    _LUA_CLAIM = (
        _LUA_JOB
        + """
        local jobs, runner, lease = ARGV[1], ARGV[2], tonumber(ARGV[3])
        local claimed = {}
        for index = 1, (#KEYS - 1) / 2 do
            local queue, waiting = KEYS[2 * index], KEYS[2 * index + 1]
            local free = tonumber(ARGV[3 + index])
            local taken = 0
            while taken < free do
                local head = redis.call('ZRANGE', queue, 0, 0)
                if #head == 0 then
                    break
                end
                local id = head[1]
                local key = jobs .. id
                if redis.call('HGET', key, 's') ~= 'queued' then
                    redis.call('ZREM', queue, id)
                    redis.call('ZREM', waiting, id)
                elseif not catch_up(key, id, queue, waiting) then
                    redis.call('ZREM', queue, id)
                    redis.call('ZREM', waiting, id)
                    redis.call('HDEL', key, 'sb')
                    start_running(key, id, runner, lease)
                    redis.call('HINCRBY', key, 'v', 1)
                    claimed[#claimed + 1] = {
                        id, record(key), redis.call('HMGET', key, 'p', 'x')
                    }
                    taken = taken + 1
                end
            end
        end
        return claimed
    """
    )
    """Claim queued jobs, oldest first per name.

    KEYS: deadlines, then queue and waiting of each name. ARGV: job key
    prefix, runner, lease in us, then the free slots of each name.
    """

    _LUA_BEAT = (
        _LUA_JOB
        + """
        local runner, lease = ARGV[1], tonumber(ARGV[2])
        local replies = {}
        for index = 1, (#KEYS - 1) / 3 do
            local key = KEYS[3 * index - 1]
            local queue, waiting = KEYS[3 * index], KEYS[3 * index + 1]
            local at = 2 + 5 * (index - 1)
            local name, id = ARGV[at + 1], ARGV[at + 2]
            local held, flag, err = 0, 0, ''
            if redis.call('HGET', key, 'n') == name then
                local ended = catch_up(key, id, queue, waiting)
                local job = redis.call(
                    'HMGET', key, 's', 'rn', 'c', 'd', 't', 'e'
                )
                if ended then
                    err = ended
                elseif job[1] == 'failed' and not job[2] then
                    err = job[6]
                end
                if job[1] == 'running' and job[2] == runner then
                    hold(key, id, lease)
                    local done, total = ARGV[at + 4], ARGV[at + 5]
                    if ARGV[at + 3] == '1'
                        and (job[4] ~= done or (job[5] or '') ~= total) then
                        write_progress(key, done, total)
                        redis.call('HINCRBY', key, 'v', 1)
                    end
                    held = 1
                    if job[3] == '1' then
                        flag = 1
                    end
                end
            end
            replies[index] = {held, flag, err}
        end
        return replies
    """
    )
    """Extend each held job's lease, write its progress, read its flag.

    KEYS: deadlines, then job, queue and waiting of each beat. ARGV: runner,
    lease in us, then name, id, has progress, done and total ('': unknown) of
    each beat. Each reply is held, flag, and the error the store ended the job
    with ('': none).
    """

    _LUA_SETTLE = (
        _LUA_JOB
        + """
        local key, queue, waiting = KEYS[2], KEYS[3], KEYS[4]
        local id, status = ARGV[2], ARGV[4]
        if redis.call('HGET', key, 'n') ~= ARGV[1] then
            return {0, ''}
        end
        local ended = catch_up(key, id, queue, waiting)
        local job = redis.call('HMGET', key, 's', 'rn', 'e')
        if not job[1] or job[2] ~= ARGV[3] then
            if ended then
                return {0, ended}
            end
            if job[1] == 'failed' and not job[2] then
                return {0, job[3]}
            end
            return {0, ''}
        end
        if job[1] ~= 'running' then
            if job[1] == status then
                return {1, ''}
            end
            return {0, ''}
        end
        if ARGV[9] == '1' then
            write_progress(key, ARGV[10], ARGV[11])
        end
        if ARGV[7] == '1' then
            redis.call('HSET', key, 'r', ARGV[8])
        end
        local err = false
        if ARGV[5] == '1' then
            err = ARGV[6]
        end
        finish(key, id, status, err, queue, waiting)
        retain(key, id)
        return {1, ''}
    """
    )
    """End a running job, and start its retention.

    KEYS: deadlines, job, queue, waiting. ARGV: name, id, runner, status, has
    error, error, has result, result, has progress, done, total ('': unknown).
    The reply is settled, and the error the store ended the job with ('':
    none).
    """

    _LUA_FIND = """
        local key, queue, waiting = KEYS[2], KEYS[3], KEYS[4]
        local id = ARGV[2]
        local stored = redis.call('HMGET', key, 'n', 'o')
        if stored[1] ~= ARGV[1] or not owns(stored[2], ARGV[3], ARGV[4]) then
            return false
        end
        catch_up(key, id, queue, waiting)
        if redis.call('EXISTS', key) == 0 then
            return false
        end
    """
    """Find the job of this name and owner, as it is now.

    KEYS: deadlines, job, queue, waiting. ARGV: name, id, has owner, owner.
    """

    _LUA_GET = (
        _LUA_JOB
        + _LUA_FIND
        + """
        return record(key)
    """
    )
    """Return the job when the owner owns it."""

    _LUA_CANCEL = (
        _LUA_JOB
        + _LUA_FIND
        + """
        local job = redis.call('HMGET', key, 's', 'c')
        if job[1] == 'queued' then
            redis.call('HSET', key, 'c', '1')
            finish(key, id, 'cancelled', false, queue, waiting)
            local ended = record(key)
            retain(key, id)
            return ended
        end
        if job[1] == 'running' and job[2] ~= '1' then
            redis.call('HSET', key, 'c', '1')
            redis.call('HINCRBY', key, 'v', 1)
        end
        return record(key)
    """
    )
    """Ask the job to stop when the owner owns it, and return it."""

    _LUA_LIST = (
        _LUA_JOB
        + """
        local own, queue, waiting = KEYS[2], KEYS[3], KEYS[4]
        local jobs, name, owner, status = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
        local limit, below = tonumber(ARGV[5]), '(' .. ARGV[6]
        local bound, scanned, last = tonumber(ARGV[7]), 0, ''
        local found = {}
        while #found <= limit and scanned < bound do
            local batch = redis.call(
                'ZREVRANGEBYSCORE', own, below, '-inf', 'WITHSCORES',
                'LIMIT', 0, math.min(limit + 1 - #found, bound - scanned)
            )
            if #batch == 0 then
                break
            end
            for index = 1, #batch, 2 do
                local id, seq = batch[index], batch[index + 1]
                scanned, last = scanned + 1, seq
                local key = jobs .. id
                local stored = redis.call('HMGET', key, 'n', 'o', 'ok')
                if stored[3] ~= own then
                    redis.call('ZREM', own, id)
                elseif stored[1] == name and stored[2] == owner then
                    catch_up(key, id, queue, waiting)
                    local now_status = redis.call('HGET', key, 's')
                    if now_status and (status == '' or now_status == status) then
                        found[#found + 1] = {seq, id, record(key)}
                    end
                end
                below = '(' .. seq
            end
        end
        if #found > limit or scanned < bound then
            last = ''
        end
        return {last, found}
    """
    )
    """List the owner's jobs, newest first, one more than the page holds.

    It reads at most `bound` members. A member whose job is gone, or no
    longer names this index, leaves it. KEYS: deadlines, owner index, queue,
    waiting. ARGV: job key prefix, name, owner, status ('': every one), limit,
    the order to list below, bound. The reply is the order of the last member
    read when the bound stopped the walk ('': it did not), and the jobs.
    """

    _LUA_PURGE = (
        _LUA_JOB
        + """
        local due = redis.call(
            'ZRANGEBYSCORE', expiries, '-inf', us(math.floor(now / 1000)),
            'LIMIT', 0, tonumber(ARGV[1])
        )
        local purged = 0
        for _, entry in ipairs(due) do
            redis.call('ZREM', expiries, entry)
            local colon = string.find(entry, ':', 1, true)
            local size = tonumber(string.sub(entry, 1, colon - 1))
            local id = string.sub(entry, colon + 1, colon + size)
            local index = string.sub(entry, colon + size + 1)
            local job = redis.call('HMGET', base .. 'id:' .. id, 'ok', 'kk')
            if job[1] ~= index and job[2] ~= index then
                purged = purged + redis.call('ZREM', index, id)
                unindex(index, id)
            end
        end
        return purged
    """
    )
    """Take expired jobs out of their owner and kept indexes, up to a limit.

    A job with the same id that the same index holds since stays. KEYS:
    deadlines. ARGV: limit. The reply is how many entries it took out.
    """

    _LUA_REAP = (
        _LUA_JOB
        + """
        local base, limit = ARGV[1], tonumber(ARGV[2])
        local reaped = 0
        while reaped < limit do
            local due = redis.call(
                'ZRANGEBYSCORE', deadlines, '-inf', us(now),
                'LIMIT', 0, limit - reaped
            )
            if #due == 0 then
                break
            end
            for _, id in ipairs(due) do
                local key = base .. 'id:' .. id
                local name = redis.call('HGET', key, 'n')
                if name and catch_up(
                    key, id, base .. 'queue:' .. name, base .. 'wait:' .. name
                ) then
                    reaped = reaped + 1
                else
                    redis.call('ZREM', deadlines, id)
                end
            end
        end
        return reaped
    """
    )
    """End jobs past a lease, `max_wait` or `max_run`, of any name.

    KEYS: deadlines. ARGV: key prefix of the store, limit.
    """

    def __init__(
        self,
        *,
        provider: Annotated[
            RedisProvider | None,
            Doc(
                """
                A pre-built `RedisProvider`. When set, the adapter
                borrows the provider's client and does not manage
                its lifecycle.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `RedisProvider` when `provider` is not set. Defaults
                to `REDIS_`. Use a custom prefix to split pools.
                """,
            ),
        ] = "REDIS_",
        prefix: Annotated[
            str,
            Doc(
                """
                Prefix for every Redis key (job store isolation), after the provider's
                `prefix`. On a Redis Cluster with no hash tag in either, the
                keys go under `{<prefix>job}:`.
                """
            ),
        ] = "",
    ) -> None:
        """Initialize the adapter."""
        if provider is None:
            self._provider = RedisProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._prefix = prefix
        self._bind_scripts()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def provider(self) -> RedisProvider:
        """The bound `RedisProvider`."""
        return self._provider

    def _bind_scripts(self) -> None:
        """(Re)register the Lua scripts against the current client."""
        self._key_prefix, marked = kind_prefix(
            self._provider, self._prefix, kind="job"
        )
        self._base = (
            self._key_prefix
            if marked
            else f"{self._key_prefix}{self._KEY_PREFIX}"
        )
        client = self._provider.client
        self._lua_submit = client.register_script(self._LUA_SUBMIT)
        self._lua_claim = client.register_script(self._LUA_CLAIM)
        self._lua_beat = client.register_script(self._LUA_BEAT)
        self._lua_settle = client.register_script(self._LUA_SETTLE)
        self._lua_get = client.register_script(self._LUA_GET)
        self._lua_cancel = client.register_script(self._LUA_CANCEL)
        self._lua_reap = client.register_script(self._LUA_REAP)
        self._lua_list = client.register_script(self._LUA_LIST)
        self._lua_purge = client.register_script(self._LUA_PURGE)

    def _rebind_provider(self, provider: RedisProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False
        self._bind_scripts()

    async def __aenter__(self) -> Self:
        """Open the adapter and its provider when owned."""
        self._loop = asyncio.get_running_loop()
        if self._owns_provider:
            await self._provider.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the provider when owned. External providers are left alone."""
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    def _keys(self, name: str, job_id: str) -> builtins.list[str]:
        """Return the deadline, job, queue and waiting keys of one job."""
        return [
            f"{self._base}deadlines",
            f"{self._base}id:{job_id}",
            f"{self._base}queue:{name}",
            f"{self._base}wait:{name}",
        ]

    def _owned_key(self, name: str, owner: str) -> str:
        """Return the key of the owner index of `name`.

        The key holds a hash of the owner, never the owner itself. The job's
        hash still stores the owner as a value, in its `o` field.
        """
        digest = hashlib.sha256(owner.encode(errors="surrogatepass"))
        return f"{self._base}own:{name}:{digest.hexdigest()[:32]}"

    async def submit(  # noqa: PLR0913
        self,
        *,
        name: str,
        job_id: str,
        owner: str | None,
        payload: str,
        context: Mapping[str, str],
        max_queued: int | None,
        max_wait: timedelta | None,
        max_run: timedelta | None,
        keep: timedelta | None,
        max_kept_succeeded: int | None,
        max_kept_failed: int | None,
        start_as: str | None,
        lease: timedelta,
    ) -> Submitted:
        """Store a job, or refuse it."""
        reply = await self._lua_submit(
            keys=[
                *self._keys(name, job_id),
                f"{self._base}order:{name}",
                *([] if owner is None else [self._owned_key(name, owner)]),
            ],
            args=[
                job_id,
                name,
                *_optional_arg(owner),
                payload,
                json.dumps(dict(context)),
                -1 if max_queued is None else max_queued,
                -1 if max_wait is None else round_up(max_wait, MICROSECOND),
                "" if keep is None else _keep_ms(keep),
                *_optional_arg(start_as),
                round_up(lease, MICROSECOND),
                "" if max_run is None else round_up(max_run, MICROSECOND),
                "" if max_kept_succeeded is None else max_kept_succeeded,
                "" if max_kept_failed is None else max_kept_failed,
            ],
            client=self._provider.client,
        )
        if reply is None:
            return Submitted(None, started=False)
        started, fields = reply
        return Submitted(_record(job_id, fields), started=bool(started))

    async def claim(
        self,
        *,
        slots: Mapping[str, int],
        runner: str,
        lease: timedelta,
    ) -> builtins.list[Claimed]:
        """Claim queued jobs up to each name's slots, oldest first per name."""
        names = [name for name, free in slots.items() if free > 0]
        if not names:
            return []
        keys = [f"{self._base}deadlines"]
        for name in names:
            keys += [f"{self._base}queue:{name}", f"{self._base}wait:{name}"]
        reply = await self._lua_claim(
            keys=keys,
            args=[
                f"{self._base}id:",
                runner,
                round_up(lease, MICROSECOND),
                *(slots[name] for name in names),
            ],
            client=self._provider.client,
        )
        return [
            Claimed(
                _record(_text(job_id), fields),
                payload=_text(payload),
                context=json.loads(_text(context)),
            )
            for job_id, fields, (payload, context) in reply
        ]

    async def beat(
        self,
        *,
        runner: str,
        beats: Sequence[Beat],
        lease: timedelta,
    ) -> builtins.list[BeatReply]:
        """Extend each held job's lease, write its progress, read its flag."""
        if not beats:
            return []
        keys = [f"{self._base}deadlines"]
        args: builtins.list[str | int] = [runner, round_up(lease, MICROSECOND)]
        for beat in beats:
            keys += self._keys(beat.name, beat.job_id)[1:]
            args += [beat.name, beat.job_id, *_progress_args(beat.progress)]
        reply = await self._lua_beat(
            keys=keys, args=args, client=self._provider.client
        )
        return [
            BeatReply(
                beat.name,
                beat.job_id,
                held=bool(held),
                cancel_requested=bool(flag),
                error=_optional(error or None),
            )
            for beat, (held, flag, error) in zip(beats, reply, strict=True)
        ]

    async def settle(
        self,
        *,
        name: str,
        job_id: str,
        runner: str,
        status: EndStatus,
        error: str | None,
        result: str | None,
        progress: ProgressUpdate | None,
    ) -> Settled:
        """End a running job, and start its retention."""
        settled, ended = await self._lua_settle(
            keys=self._keys(name, job_id),
            args=[
                name,
                job_id,
                runner,
                status.value,
                *_optional_arg(error),
                *_optional_arg(result),
                *_progress_args(progress),
            ],
            client=self._provider.client,
        )
        return Settled(settled=bool(settled), error=_optional(ended or None))

    async def get(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Return the job when `owner` owns it, else `None`."""
        fields = await self._lua_get(
            keys=self._keys(name, job_id),
            args=[name, job_id, *_optional_arg(owner)],
            client=self._provider.client,
        )
        return None if fields is None else _record(job_id, fields)

    async def cancel(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Ask the job to stop when `owner` owns it, and return it."""
        fields = await self._lua_cancel(
            keys=self._keys(name, job_id),
            args=[name, job_id, *_optional_arg(owner)],
            client=self._provider.client,
        )
        return None if fields is None else _record(job_id, fields)

    async def list(
        self,
        *,
        name: str,
        owner: str,
        status: JobStatus | None,
        limit: int,
        after: str | None,
    ) -> Listed:
        """Return the jobs of `name` that `owner` owns, newest submitted first.

        One script walks the owner index from the newest, makes the move a
        read makes on each job, and drops the members whose job is gone.
        """
        below = FIRST if after is None else read_cursor(after)
        deadlines, _, queue, waiting = self._keys(name, "")
        stopped, found = await self._lua_list(
            keys=[deadlines, self._owned_key(name, owner), queue, waiting],
            args=[
                f"{self._base}id:",
                name,
                owner,
                "" if status is None else status.value,
                limit,
                below,
                scan_limit(limit),
            ],
            client=self._provider.client,
        )
        return page_of(
            [
                (int(_text(seq)), _record(_text(job_id), fields))
                for seq, job_id, fields in found
            ],
            limit,
            int(_text(stopped)) if stopped else None,
        )

    async def reap(self, *, limit: int) -> int:
        """End jobs past a lease, `max_wait` or `max_run`, of any name."""
        return int(
            await self._lua_reap(
                keys=[f"{self._base}deadlines"],
                args=[self._base, limit],
                client=self._provider.client,
            )
        )

    async def purge(self, *, limit: int) -> int:
        """Forget ended jobs past their retention, whatever their name.

        Redis expires an ended job on its own once `keep` has passed. This
        takes up to `limit` such jobs out of their owner and kept indexes,
        and returns how many index entries it took out.
        """
        return int(
            await self._lua_purge(
                keys=[f"{self._base}deadlines"],
                args=[limit],
                client=self._provider.client,
            )
        )


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _optional_arg(value: str | None) -> tuple[str, str]:
    """Return a script's flag and value for text that may be `None`."""
    return ("0", "") if value is None else ("1", value)


def _progress_args(progress: ProgressUpdate | None) -> tuple[str, str, str]:
    """Return a script's flag, done and total for counts that may be `None`."""
    if progress is None:
        return ("0", "", "")
    total = "" if progress.total is None else str(progress.total)
    return ("1", str(progress.done), total)


def _keep_ms(keep: timedelta) -> int:
    """Return the retention in whole milliseconds, rounded up.

    Zero keeps nothing. A positive retention is at least one millisecond.
    """
    if keep <= timedelta(0):
        return 0
    return round_up(keep, MILLISECOND)


def _text(value: bytes | str) -> str:
    """Decode a Redis reply value to `str`."""
    return value.decode() if isinstance(value, bytes) else value


def _optional(value: bytes | str | None) -> str | None:
    """Decode a Redis field that may be missing."""
    return None if value is None else _text(value)


def _required(value: bytes | str | None, job_id: str) -> str:
    """Decode a Redis field every stored job has."""
    if value is None:
        msg = f"unexpected missing field in stored job {job_id!r}"
        raise ValueError(msg)
    return _text(value)


def _time(value: bytes | str | None) -> datetime | None:
    """Read a stored count of microseconds since the epoch as a datetime."""
    if value is None:
        return None
    return _EPOCH + timedelta(microseconds=int(_text(value)))


def _record(job_id: str, fields: Sequence[bytes | str | None]) -> JobRecord:
    """Build a `JobRecord` from the fields a script returns."""
    (
        name,
        status,
        version,
        done,
        total,
        result,
        error,
        cancel,
        created_at,
        started_at,
        finished_at,
    ) = fields
    return JobRecord(
        id=job_id,
        name=_required(name, job_id),
        status=JobStatus(_required(status, job_id)),
        version=int(_required(version, job_id)),
        done=int(_required(done, job_id)),
        total=None if total is None else int(_text(total)),
        result=_optional(result),
        error=_optional(error),
        cancel_requested=_optional(cancel) == "1",
        created_at=_EPOCH
        + timedelta(microseconds=int(_required(created_at, job_id))),
        started_at=_time(started_at),
        finished_at=_time(finished_at),
    )
