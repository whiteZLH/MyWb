"""Bounded request counters and the request log; never store prompts, responses or keys.

Beyond the request/success/duration counters the overview page already shows,
this module folds the usage records the upstream reports (prompt-prefix cache
hits, reasoning tokens, decode throughput) into per-day, per-model aggregates
that back the console's runtime-stats page. Only numbers are retained: prompts,
completions and API keys never reach the aggregation, and nothing here is ever
written back into a response.

Aggregates and the most recent 100 request records are persisted through
``admin.db.Database`` (SQLite); older installs are migrated from
``metrics.json`` on first start and the legacy file is then removed.
"""
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

PATHS = {"/v1/chat/completions", "/v1/responses", "/v1/messages"}

# 账号展示名通过 ASGI scope 在中间件间传递：PoolMiddleware（内层）在选定账号后
# 写入 scope["account_label"]，MetricsMiddleware（外层）在记录时读取。scope 是每个
# 请求独享的可变字典，无需担心并发串扰或上下文重置时序。

# Day buckets follow the same Beijing calendar the account pool uses for
# check-ins, so "today" means the same thing everywhere in the console.
CN = timezone(timedelta(hours=8))

# Bounded TTFT histogram: exact counts per bucket, percentiles interpolated
# inside the bucket a sample lands in. Keeps persistence flat regardless of
# request volume.
TTFT_BUCKETS_MS = (100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 7500, 10000, 15000, 20000, 30000, 60000, 120000)

# History is bounded so a long-lived process cannot grow the database without limit.
MAX_DAYS = 90

_BUCKET_KEYS = (
    "requests", "success", "http_success", "api_requests", "test_requests",
    "duration_ms", "ttft_ms", "ttft_count", "decode_ms", "decode_tokens",
    "prompt_tokens", "completion_tokens", "reasoning_tokens", "cache_hit_tokens",
)


def day_key(timestamp=None):
    """Beijing calendar day of `timestamp` (defaults to now)."""
    moment = datetime.fromtimestamp(time.time() if timestamp is None else timestamp, CN)
    return moment.strftime("%Y-%m-%d")


def _as_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _as_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, float(value))


def _detail(usage, *names):
    for name in names:
        value = usage.get(name)
        if isinstance(value, dict):
            return value
    return {}


def normalize_usage(usage):
    """Fold every upstream usage dialect into one counter shape.

    Accepts OpenAI chat (`prompt_tokens` + `prompt_cache_hit_tokens`), Responses
    (`input_tokens` + `input_tokens_details.cached_tokens`) and Anthropic
    (`input_tokens` + `cache_read_input_tokens`) records, and returns None when
    the record carries nothing countable.
    """
    if not isinstance(usage, dict) or not usage:
        return None
    prompt = _as_int(usage.get("prompt_tokens")) or _as_int(usage.get("input_tokens"))
    completion = _as_int(usage.get("completion_tokens")) or _as_int(usage.get("output_tokens"))
    total = _as_int(usage.get("total_tokens")) or (prompt + completion)
    prompt_details = _detail(usage, "prompt_tokens_details", "input_tokens_details")
    completion_details = _detail(usage, "completion_tokens_details", "output_tokens_details")
    cached = (
        _as_int(usage.get("prompt_cache_hit_tokens"))
        or _as_int(usage.get("cache_read_input_tokens"))
        or _as_int(usage.get("cached_tokens"))
        or _as_int(prompt_details.get("cached_tokens"))
    )
    reasoning = (
        _as_int(usage.get("completion_thinking_tokens"))
        or _as_int(usage.get("reasoning_tokens"))
        or _as_int(completion_details.get("reasoning_tokens"))
    )
    if not any((prompt, completion, total, cached, reasoning)):
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_hit_tokens": min(cached, prompt) if prompt else cached,
        "reasoning_tokens": reasoning,
    }


def merge_usage(target, found):
    """Merge one usage record into `target`, preferring recorded non-zero values.

    Streaming adapters report usage more than once (Anthropic sends a zeroed
    `message_start` before the real `message_delta`), so later zeroes must not
    erase what earlier frames already established.
    """
    if not isinstance(found, dict):
        return
    for key, value in found.items():
        if isinstance(value, dict):
            nested = target.get(key)
            if not isinstance(nested, dict):
                nested = {}
                target[key] = nested
            for inner, inner_value in value.items():
                if inner_value:
                    nested[inner] = inner_value
        elif value:
            target[key] = value


def usage_from_event(value):
    """Pull the usage record out of any SSE payload or assembled JSON body."""
    if not isinstance(value, dict):
        return None
    event_type = value.get("type")
    if event_type == "message_start":
        message = value.get("message")
        return message.get("usage") if isinstance(message, dict) else None
    if event_type == "message_delta":
        return value.get("usage")
    if event_type in ("response.completed", "response.incomplete", "response.failed"):
        response = value.get("response")
        return response.get("usage") if isinstance(response, dict) else None
    usage = value.get("usage")
    return usage if isinstance(usage, dict) else None


def carries_token(value):
    """True when a payload carries visible generation content (text or reasoning).

    This is the first-token signal for TTFT: the first frame with something to
    render, whichever protocol produced it.
    """
    if not isinstance(value, dict):
        return False
    event_type = value.get("type")
    if event_type in ("response.output_text.delta", "response.reasoning_summary_text.delta"):
        return bool(value.get("delta"))
    if event_type == "content_block_delta":
        delta = value.get("delta")
        if not isinstance(delta, dict):
            return False
        return bool(delta.get("text") or delta.get("thinking") or delta.get("partial_json"))
    if event_type == "content_block_start":
        block = value.get("content_block")
        return isinstance(block, dict) and block.get("type") == "tool_use"
    choices = value.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                continue
            if delta.get("content") or delta.get("tool_calls"):
                return True
            for key in ("reasoning_content", "reasoning", "thinking", "thinking_content", "reasoning_text"):
                if delta.get(key):
                    return True
    return False


def _new_bucket():
    bucket = {key: 0 for key in _BUCKET_KEYS}
    bucket["duration_ms"] = 0.0
    bucket["ttft_ms"] = 0.0
    bucket["decode_ms"] = 0.0
    bucket["ttft_hist"] = [0] * (len(TTFT_BUCKETS_MS) + 1)
    return bucket


def _adopt(raw):
    """Rebuild a persisted bucket, discarding anything unrecognised or invalid."""
    bucket = _new_bucket()
    if not isinstance(raw, dict):
        return bucket
    for key in _BUCKET_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        bucket[key] = max(0.0, float(value))
    hist = raw.get("ttft_hist")
    if isinstance(hist, list) and len(hist) == len(bucket["ttft_hist"]):
        bucket["ttft_hist"] = [max(0, _as_int(count)) for count in hist]
    return bucket


def _accumulate(target, source):
    for key in _BUCKET_KEYS:
        target[key] += source[key]
    hist = target["ttft_hist"]
    for index, count in enumerate(source["ttft_hist"]):
        hist[index] += count


def _hist_index(milliseconds):
    for index, edge in enumerate(TTFT_BUCKETS_MS):
        if milliseconds <= edge:
            return index
    return len(TTFT_BUCKETS_MS)


def _percentile(hist, total, fraction):
    """Bucket-interpolated percentile of a bounded histogram, in milliseconds."""
    if total <= 0:
        return None
    target = total * fraction
    seen = 0
    for index, count in enumerate(hist):
        if seen + count >= target:
            lower = 0.0 if index == 0 else float(TTFT_BUCKETS_MS[index - 1])
            upper = float(TTFT_BUCKETS_MS[index]) if index < len(TTFT_BUCKETS_MS) else lower
            if upper <= lower or count <= 0:
                return lower
            return lower + (upper - lower) * ((target - seen) / count)
        seen += count
    return float(TTFT_BUCKETS_MS[-1])


def _record(bucket, usage, ttft_ms, duration_ms, ok, http_ok, source):
    bucket["requests"] += 1
    bucket["success"] += int(bool(ok))
    bucket["http_success"] += int(bool(http_ok))
    bucket["api_requests"] += int(source == "api")
    bucket["test_requests"] += int(source == "test")
    bucket["duration_ms"] += max(0.0, duration_ms)
    if ttft_ms is not None:
        ttft = max(0.0, float(ttft_ms))
        bucket["ttft_ms"] += ttft
        bucket["ttft_count"] += 1
        bucket["ttft_hist"][_hist_index(ttft)] += 1
    if usage:
        bucket["prompt_tokens"] += usage["prompt_tokens"]
        bucket["completion_tokens"] += usage["completion_tokens"]
        bucket["reasoning_tokens"] += usage["reasoning_tokens"]
        bucket["cache_hit_tokens"] += usage["cache_hit_tokens"]
        # Decode throughput is only meaningful where the first token was timed
        # and the provider reported output tokens, mirroring the console's
        # per-request figures.
        if ttft_ms is not None and usage["completion_tokens"] > 0 and duration_ms > ttft_ms:
            bucket["decode_ms"] += duration_ms - ttft_ms
            bucket["decode_tokens"] += usage["completion_tokens"]


def _view(bucket):
    requests = int(bucket["requests"])
    prompt_tokens = int(bucket["prompt_tokens"])
    cache_hit = int(bucket["cache_hit_tokens"])
    denominator = prompt_tokens or cache_hit
    samples = int(bucket["ttft_count"])
    decode_ms = bucket["decode_ms"]
    p50 = _percentile(bucket["ttft_hist"], samples, 0.5)
    return {
        "requests": requests,
        "success": int(bucket["success"]),
        "failed": requests - int(bucket["success"]),
        "success_rate": round(100 * bucket["success"] / requests, 1) if requests else None,
        "http_success_rate": round(100 * bucket["http_success"] / requests, 1) if requests else None,
        "api_requests": int(bucket["api_requests"]),
        "test_requests": int(bucket["test_requests"]),
        "avg_duration_ms": round(bucket["duration_ms"] / requests) if requests else None,
        "cache_hit_rate": min(100.0, round(100 * cache_hit / denominator, 1)) if denominator else None,
        "cache_hit_tokens": cache_hit,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": int(bucket["completion_tokens"]),
        "reasoning_tokens": int(bucket["reasoning_tokens"]),
        "total_tokens": prompt_tokens + int(bucket["completion_tokens"]),
        "ttft_avg_ms": round(bucket["ttft_ms"] / samples) if samples else None,
        "ttft_p50_ms": round(p50) if p50 is not None else None,
        "ttft_samples": samples,
        "tokens_per_second": round(bucket["decode_tokens"] / (decode_ms / 1000), 1) if decode_ms > 0 and bucket["decode_tokens"] else None,
        "decode_tokens": int(bucket["decode_tokens"]),
    }


class RequestMetrics:
    def __init__(self, db=None, legacy_path=None):
        self.lock = threading.Lock()
        self.started_at = int(time.time())
        self.in_flight = self.total = self.success = self.http_success = 0
        self.duration_sum = 0
        self.api_count = self.test_count = 0
        self.db = db
        self.recent = deque(maxlen=100)
        self.days = {}
        self.dirty = False
        if db is not None and legacy_path is not None:
            self._migrate_legacy(Path(legacy_path))
        self._load()

    def _migrate_legacy(self, path):
        """把旧 metrics.json 的按天聚合导入数据库，成功后删除原文件。"""
        if not path.exists():
            return
        if self.db.load_metric_days():
            try:
                path.unlink()
            except OSError:
                pass
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            days = data.get("days") if isinstance(data, dict) else None
            if isinstance(days, dict):
                self.db.replace_metric_days(days)
        except (ValueError, OSError):
            return
        try:
            path.unlink()
        except OSError:
            pass

    # ---- persistence ----

    def _load(self):
        if self.db is None:
            return
        try:
            days = self.db.load_metric_days()
        except Exception:
            return
        for key, value in days.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            models = {}
            for name, bucket in (value.get("models") or {}).items():
                if isinstance(name, str) and name:
                    models[name[:80]] = _adopt(bucket)
            accounts = {}
            for name, bucket in (value.get("accounts") or {}).items():
                if isinstance(name, str) and name:
                    accounts[name[:80]] = _adopt(bucket)
            self.days[key] = {"all": _adopt(value.get("all")), "models": models, "accounts": accounts}
        try:
            self.recent = deque(self.db.request_log(100), maxlen=100)
        except Exception:
            pass

    def _prune(self):
        for key in sorted(self.days)[:-MAX_DAYS]:
            del self.days[key]

    def save(self):
        """Flush pending counters; a no-op when nothing changed since the last call."""
        if self.db is None:
            return
        with self.lock:
            if not self.dirty:
                return
            self._prune()
            days = self.days
            self.dirty = False
        try:
            self.db.upsert_metric_days(days)
            self.db.prune_metric_days(MAX_DAYS)
        except Exception:
            with self.lock:
                self.dirty = True

    # ---- recording ----

    def begin(self):
        with self.lock:
            self.in_flight += 1

    def finish(self, path, source, status, ok, duration, outcome, usage=None, ttft_ms=None, model=None,
               streaming=False, key_label="", account=""):
        duration_ms = float(duration)
        normalized = normalize_usage(usage)
        http_ok = status is not None and 200 <= status < 300
        record = {
            "time": int(time.time()), "path": path, "source": source,
            "status": status, "ok": ok, "duration_ms": round(duration_ms),
            "outcome": outcome, "model": model or "",
            "ttft_ms": round(ttft_ms) if ttft_ms is not None else None,
            "streaming": bool(streaming), "key": key_label or "",
            # 账号展示名（非凭据），用于按账号维度汇总；未识别时为空串。
            "account": account or "",
            # Only numbers survive: the token tally mirrors the aggregates and
            # never carries prompt or completion text.
            "tokens": None if normalized is None else {
                "prompt": normalized["prompt_tokens"],
                "completion": normalized["completion_tokens"],
                "total": normalized["total_tokens"],
                "cached": normalized["cache_hit_tokens"],
                "reasoning": normalized["reasoning_tokens"],
            },
        }
        with self.lock:
            self.in_flight -= 1
            self.total += 1
            self.success += int(ok)
            self.http_success += int(http_ok)
            self.duration_sum += duration_ms
            self.api_count += int(source == "api")
            self.test_count += int(source == "test")
            self.recent.appendleft(record)
            day = self.days.setdefault(day_key(), {"all": _new_bucket(), "models": {}, "accounts": {}})
            _record(day["all"], normalized, ttft_ms, duration_ms, ok, http_ok, source)
            if model:
                bucket = day["models"].setdefault(model[:80], _new_bucket())
                _record(bucket, normalized, ttft_ms, duration_ms, ok, http_ok, source)
            if account:
                bucket = day["accounts"].setdefault(account[:80], _new_bucket())
                _record(bucket, normalized, ttft_ms, duration_ms, ok, http_ok, source)
            self.dirty = True
        if self.db is not None:
            try:
                self.db.add_request_log(record)
                self.db.prune_request_log(100)
            except Exception:
                pass

    # ---- views ----

    def snapshot(self):
        with self.lock:
            return {"started_at": self.started_at, "completed": self.total, "in_flight": self.in_flight,
                    "succeeded": self.success, "failed": self.total - self.success,
                    "success_rate": round(100*self.success/self.total, 1) if self.total else None,
                    "http_success_rate": round(100*self.http_success/self.total, 1) if self.total else None,
                    "avg_duration_ms": round(self.duration_sum/self.total) if self.total else None,
                    "api_count": self.api_count, "test_count": self.test_count, "recent": list(self.recent)}

    def stats(self):
        """Today's and whole-history aggregates, each split by model and by account."""
        with self.lock:
            today = day_key()
            today_day = self.days.get(today)
            overall, models, accounts = _new_bucket(), {}, {}
            for day in self.days.values():
                _accumulate(overall, day["all"])
                for name, bucket in day["models"].items():
                    _accumulate(models.setdefault(name, _new_bucket()), bucket)
                for name, bucket in day.get("accounts", {}).items():
                    _accumulate(accounts.setdefault(name, _new_bucket()), bucket)
            first_day = min(self.days) if self.days else None
        today_day = today_day or {}
        return {
            "today": {"date": today, "days": 1, "since": today,
                      **_view(today_day.get("all") or _new_bucket()),
                      "models": _rank(today_day.get("models") or {}),
                      "accounts": _rank(today_day.get("accounts") or {})},
            "all": {"date": today, "days": len(self.days), "since": first_day,
                    **_view(overall), "models": _rank(models), "accounts": _rank(accounts)},
        }


def _rank(models):
    rows = [{"model": name, **_view(bucket)} for name, bucket in models.items()]
    rows.sort(key=lambda row: (-row["requests"], row["model"]))
    return rows


class MetricsMiddleware:
    def __init__(self, app, metrics, source="api", key_label=None, account=""):
        self.app, self.metrics, self.source = app, metrics, source
        # 可选的请求密钥标签解析器：headers -> 展示名。默认不记录任何密钥信息。
        self.key_label = key_label
        # 缺省账号展示名：用于不经过 PoolMiddleware 的调用（如后台连接测试）。
        self.account = account

    async def __call__(self, scope, receive, send):
        path = scope.get("path")
        if scope["type"] != "http" or scope["method"] != "POST" or path not in PATHS:
            return await self.app(scope, receive, send)
        start = time.monotonic()
        self.metrics.begin()
        status = None
        completed = failed = streaming = terminal = disconnected = False
        buffer = b""
        oversized_line = False
        nonstream = bytearray()
        usage = {}
        model = None
        first_token = None
        try:
            key_label = self.key_label(dict(scope.get("headers", []))) if self.key_label else ""
        except Exception:
            key_label = ""

        def absorb(value):
            nonlocal model, first_token
            if not isinstance(value, dict):
                return
            candidate = value.get("model")
            response = value.get("response")
            if not isinstance(candidate, str) and isinstance(response, dict):
                candidate = response.get("model")
            if not isinstance(candidate, str) and isinstance(value.get("message"), dict):
                candidate = value["message"].get("model")
            if isinstance(candidate, str) and candidate:
                model = candidate
            merge_usage(usage, usage_from_event(value))
            if first_token is None and carries_token(value):
                first_token = time.monotonic()

        def event(line):
            nonlocal failed, terminal
            if not line.startswith(b"data:"):
                return
            payload = line[5:].strip()
            if payload == b"[DONE]":
                terminal = True
                return
            try:
                value = json.loads(payload)
            except ValueError:
                return
            if not isinstance(value, dict):
                return
            absorb(value)
            typ = value.get("type")
            if value.get("error") or typ in ("error", "response.failed", "response.incomplete"):
                failed = True
            if typ in ("response.completed", "message_stop"):
                terminal = True
            response = value.get("response")
            if isinstance(response, dict) and (response.get("error") or response.get("status") in ("failed", "incomplete")):
                failed = True

        def settle(payload):
            try:
                absorb(json.loads(payload))
            except ValueError:
                pass

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect" and not completed:
                disconnected = True
            return message

        async def observed_send(message):
            nonlocal status, streaming, completed, buffer, failed, oversized_line, nonstream
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = dict(message.get("headers", []))
                streaming = b"text/event-stream" in headers.get(b"content-type", b"").lower()
            elif message["type"] == "http.response.body":
                if streaming:
                    # Bound storage even if the upstream sends a huge unterminated line.
                    for fragment in message.get("body", b"").splitlines(keepends=True):
                        end = fragment.endswith(b"\n")
                        if not oversized_line and len(buffer) + len(fragment) <= 65536:
                            buffer += fragment
                        else:
                            oversized_line = True
                            buffer = b""
                        if end:
                            if not oversized_line:
                                event(buffer)
                            buffer = b""
                            oversized_line = False
                elif len(nonstream) < 262144:
                    nonstream += message.get("body", b"")[:262144 - len(nonstream)]
                if not message.get("more_body", False):
                    if streaming and buffer:
                        event(buffer)
                    elif not streaming and nonstream:
                        settle(nonstream)
                    await send(message)
                    completed = True
                    return
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException:
            failed = True
            raise
        finally:
            http_ok = status is not None and 200 <= status < 300
            ok = http_ok and completed and not disconnected and not failed and (not streaming or terminal)
            outcome = "success" if ok else "stream_error" if failed and streaming else "interrupted" if not completed or disconnected or (streaming and not terminal) else "http_error"
            # A non-streaming body only lands once, so its arrival cannot be told
            # apart from the response's end: TTFT is a streaming-only reading.
            ttft = (first_token - start) * 1000 if streaming and first_token is not None else None
            # PoolMiddleware（内层）在选定账号后写入 scope；未走账号池时回退到
            # 中间件自身的缺省账号（如后台连接测试的手动账号）。
            account = scope.get("account_label") or self.account or ""
            self.metrics.finish(path, self.source, status, ok, (time.monotonic()-start)*1000, outcome,
                                usage=usage or None, model=model, ttft_ms=ttft,
                                streaming=streaming, key_label=key_label, account=account)
