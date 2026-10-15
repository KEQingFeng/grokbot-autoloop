"""Local, rule-only feedback intake. Public entry point: handle(ctx, args)."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from datetime import datetime, timezone


_SOURCES = {"github", "discord", "x", "local"}
_LANES = {"github", "social", "maintenance"}
_AD_REPLY = "感谢关注，日后会出相关合作方案，敬请期待"
_INJECTION = re.compile(
    r"ignore\s+(?:(?:all|the)\s+)?(?:previous|prior|above)\s+instructions|"
    r"(?:disregard|override)\s+(?:all\s+)?(?:instructions|rules|system)|"
    r"(?:system|developer)\s*(?:prompt|message)\s*:|"
    r"(?:bypass|skip)\s+(?:the\s+)?(?:approval|safety)|"
    r"忽略.{0,12}(?:指令|规则)|跳过.{0,6}(?:审批|审核)|泄露.{0,6}(?:密钥|提示词)",
    re.IGNORECASE,
)


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _read(path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _scrub(value):
    from autoloop import scrub_secrets

    return scrub_secrets(value)


def _review_credentials(record):
    clean = _scrub(record)
    if clean != record:
        reasons = clean.setdefault("review_reasons", [])
        if "credential_redacted" not in reasons:
            reasons.append("credential_redacted")
        clean["needs_review"] = True
    return clean


def _collection(ctx, name):
    rows = _read(Path(ctx.alo) / name, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid collection: " + name)
    if name == "feedback.json":
        return [_review_credentials(row) for row in rows]
    return _scrub(rows)


def _commit(ctx, changes):
    """Stage every JSON document before replacing any; roll back write errors."""
    directory = Path(ctx.alo)
    if not directory.is_dir():
        raise ValueError(".autoloop directory is not initialized")
    staged = {}
    originals = {}
    replaced = []
    try:
        for name, value in changes.items():
            target = directory / name
            if target.is_symlink():
                raise ValueError("refusing symlink store: " + name)
            originals[target] = target.read_bytes() if target.exists() else None
            payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            descriptor, temporary = tempfile.mkstemp(prefix=".feedback-", dir=directory)
            staged[target] = Path(temporary)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        for target, temporary in staged.items():
            os.replace(temporary, target)
            replaced.append(target)
    except Exception:
        for target in reversed(replaced):
            original = originals[target]
            if original is None:
                target.unlink()
            else:
                target.write_bytes(original)
        raise
    finally:
        for temporary in staged.values():
            if temporary.exists():
                temporary.unlink()


def _text(row, name, default=""):
    value = row.get(name, default)
    if not isinstance(value, str):
        raise ValueError(name + " must be a string")
    return value


def _strings(row, name):
    values = row.get(name, [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(name + " must be an array of strings")
    return values


def _paths(ctx, values):
    root = Path(ctx.root).resolve()
    unknown = []
    for value in values:
        path = PurePosixPath(value)
        if (not value or value.strip() != value or path.is_absolute()
                or ".." in path.parts or not path.parts
                or any(part in {".git", ".autoloop"} for part in path.parts)
                or re.search(r"[\\:*?\[\]{}~$`;|&<>\x00-\x1f\x7f]", value)):
            raise ValueError("unsafe path: " + repr(value))
        resolved = (root / value).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("path escapes repository: " + value)
        if any(part in {".git", ".autoloop"} for part in resolved.relative_to(root).parts):
            raise ValueError("path aliases repository control data: " + value)
        if not resolved.exists():
            unknown.append(value)
    return unknown


def _term_matches(text, term):
    term = term.strip()
    if not term:
        return False
    left = r"(?<![a-z0-9_])" if re.match(r"[a-z0-9_]", term, re.I) else ""
    right = r"(?![a-z0-9_])" if re.search(r"[a-z0-9_]$", term, re.I) else ""
    return re.search(left + re.escape(term) + right, text, re.I) is not None


def _category(text, lane, terms):
    if ("中转站" in text and any(word in text for word in ("广告", "低价", "充值", "优惠", "推广"))
            or re.search(r"\b(?:buy now|promo code|api reseller)\b", text, re.I)):
        return "advertisement"
    if lane == "social" and not any(_term_matches(text, term) for term in terms):
        return "irrelevant"
    if re.search(r"\b(?:bug|crash(?:es|ed)?|error|broken|fails?|failure)\b|报错|崩溃|故障|无法启动", text, re.I):
        return "bug"
    if re.search(r"\b(?:feature|please add|support|request|enhancement)\b|希望|新增|增加|功能建议", text, re.I):
        return "feature"
    if re.search(r"\b(?:how|why|question)\b|[?？]|如何|怎么|请问", text, re.I):
        return "question"
    return "uncertain"


def _prepare_import(ctx, file, lane):
    if not file:
        raise ValueError("file is required")
    with Path(file).open(encoding="utf-8") as stream:
        incoming = json.load(stream)
    if isinstance(incoming, dict):
        incoming = incoming.get("items", [incoming])
    if not isinstance(incoming, list) or len(incoming) > 100:
        raise ValueError("expected at most 100 feedback records")
    config = getattr(ctx, "config", None)
    if config is None:
        config = getattr(ctx, "cfg", {})
    terms = (config.get("feedback") or {}).get("project_terms") or []
    if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
        raise ValueError("feedback.project_terms must be an array of strings")
    normalized = []
    for raw in incoming:
        if not isinstance(raw, dict):
            raise ValueError("feedback record must be an object")
        source = _text(raw, "source")
        source_id = raw.get("source_id")
        if (source not in _SOURCES or isinstance(source_id, bool)
                or not isinstance(source_id, (str, int)) or not str(source_id).strip()):
            raise ValueError("known source and nonempty source_id required")
        source_id = str(source_id).strip()
        title, body = _text(raw, "title"), _text(raw, "body")
        if len(body) > 16000:
            raise ValueError("body exceeds 16000 characters")
        paths = _strings(raw, "paths")
        unknown = _paths(ctx, paths)
        acceptance = _strings(raw, "acceptance")
        record = {"source": source, "source_id": source_id, "title": title,
                  "body": body, "url": _text(raw, "url"), "author": _text(raw, "author"),
                  "paths": paths, "acceptance": acceptance}
        identity = json.dumps([source, source_id], ensure_ascii=False, separators=(",", ":"))
        record["id"] = "fb-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        record["lane"] = lane or ("social" if source in {"discord", "x"} else "github")
        text = title + "\n" + body
        record["category"] = _category(text, record["lane"], terms)
        reasons = []
        if not paths:
            reasons.append("missing_paths")
        if unknown:
            reasons.append("unknown_paths")
        if _INJECTION.search(json.dumps(raw, ensure_ascii=False)):
            reasons.append("prompt_injection")
        if "classification" in raw:
            record["classification_metadata"] = raw["classification"]
            reasons.append("untrusted_classification")
        record["unknown_paths"] = unknown
        record["review_reasons"] = reasons
        record["needs_review"] = bool(reasons)
        record["reply_draft"] = _AD_REPLY if record["category"] == "advertisement" else None
        record["updated_at"] = _stamp()
        normalized.append(_review_credentials(record))
    rows = _collection(ctx, "feedback.json")
    positions = {row.get("id"): index for index, row in enumerate(rows)}
    for record in normalized:
        key = record["id"]
        if key in positions:
            old = rows[positions[key]]
            record["created_at"] = old.get("created_at", record["updated_at"])
            if old.get("proposal_id"):
                record["proposal_id"] = old["proposal_id"]
            rows[positions[key]] = record
        else:
            record["created_at"] = record["updated_at"]
            positions[key] = len(rows)
            rows.append(record)
    return rows, len(normalized)


def _promote(ctx, item_id):
    rows = _collection(ctx, "feedback.json")
    item = next((row for row in rows if row.get("id") == item_id), None)
    if item is None:
        raise ValueError("feedback id not found")
    queue = _collection(ctx, "queue.json")
    if item.get("proposal_id"):
        return {"result": "already_promoted", "id": item["id"], "proposal_id": item["proposal_id"]}
    if (item.get("category") not in {"bug", "feature"} or item.get("needs_review")
            or not item.get("paths") or _paths(ctx, item["paths"])):
        raise ValueError("feedback is not eligible for promotion; human review required")
    proposal_id = "feedback-" + item["id"][3:]
    existing = next((task for task in queue if task.get("id") == proposal_id
                     or task.get("feedback_id") == item_id), None)
    if existing:
        proposal_id = existing["id"]
    else:
        queue.append({"id": proposal_id, "title": item["title"], "body": item["body"],
                      "kind": "bugfix" if item["category"] == "bug" else "new_feature",
                      "paths": item["paths"], "priority": 3, "acceptance": item.get("acceptance", []),
                      "source": item["source"], "source_id": item["source_id"], "url": item["url"],
                      "feedback_id": item_id, "policy": "needs_approval", "approved": False,
                      "status": "open", "deduplication": item_id})
    item["proposal_id"] = proposal_id
    _commit(ctx, {"queue.json": queue, "feedback.json": rows})
    return {"result": "promoted", "id": item_id, "proposal_id": proposal_id}


def _lessons(ctx):
    rows = _collection(ctx, "lessons.json")
    seen = {row.get("event_id") for row in rows}
    path = Path(ctx.alo) / "events.jsonl"
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event must be an object")
                event_id, kind = event.get("event_id"), event.get("kind", "")
                if not isinstance(kind, str):
                    continue
                exit_code = event.get("exit", event.get("exit_code"))
                if (not isinstance(event_id, str) or not event_id or event_id in seen
                        or not (kind in {"failed", "failure", "blocked", "gate_red"}
                                or kind.endswith(("_failed", "_failure", "_blocked"))
                                or (kind == "job_exit" and exit_code not in (None, 0)))):
                    continue
                rows.append({"event_id": event_id, "kind": kind,
                             "task_id": event.get("task_id"), "evidence": _scrub(event),
                             "observation": _scrub(event.get("red") if kind == "gate_red" else
                                                   event.get("reason") or event.get("error") or kind),
                             "suggestion": "Review this event's evidence and confirm a corrective action.",
                             "status": "pending_confirmation", "automatic_rule": False})
                seen.add(event_id)
    _commit(ctx, {"lessons.json": rows})
    return {"result": "lessons", "items": rows}


def handle(ctx, args) -> int:
    """Dispatch import/list/promote/lessons/cycle; print one JSON result and return an exit code.

    Accepts the engine's Ctx (cfg) or a caller exposing config. All input is local
    and untrusted. Classification metadata never grants approval. No network,
    model, subprocess, external messages, or automatic lesson rules are used.
    """
    try:
        action = getattr(args, "action", None)
        lane = getattr(args, "lane", None)
        file = getattr(args, "file", None)
        if lane is not None and lane not in _LANES:
            raise ValueError("unknown lane")
        code = 0
        if action == "list":
            rows = _collection(ctx, "feedback.json")
            item_id = getattr(args, "id", None)
            result = {"result": "listed", "items": [row for row in rows
                      if (lane is None or row.get("lane") == lane)
                      and (item_id is None or row.get("id") == item_id)]}
        elif action == "import":
            if lane == "maintenance":
                raise ValueError("maintenance does not import feedback")
            rows, count = _prepare_import(ctx, file, lane)
            _commit(ctx, {"feedback.json": rows})
            result = {"result": "imported", "count": count, "items": rows}
        elif action == "promote":
            result = _promote(ctx, getattr(args, "id", None))
        elif action == "lessons":
            result = _lessons(ctx)
        elif action == "cycle":
            if lane not in _LANES:
                raise ValueError("cycle requires a lane")
            started = _stamp()
            changes = {}
            if lane == "maintenance":
                result = {"result": "maintenance_required", "lane": lane}
            elif not file:
                result = {"result": "source_not_configured", "lane": lane}
                code = 1
            else:
                rows, count = _prepare_import(ctx, file, lane)
                changes["feedback.json"] = rows
                result = {"result": "imported", "lane": lane, "count": count}
            loops = _read(Path(ctx.alo) / "loops.json", {})
            if not isinstance(loops, dict):
                raise ValueError("invalid loops.json")
            loops[lane] = dict(result, started_at=started, finished_at=_stamp())
            changes["loops.json"] = loops
            _commit(ctx, changes)
        else:
            raise ValueError("unknown feedback action")
        print(json.dumps(_scrub(result), ensure_ascii=False, allow_nan=False))
        return code
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(json.dumps(_scrub({"result": "error", "error": str(exc)}), ensure_ascii=False))
        return 1
