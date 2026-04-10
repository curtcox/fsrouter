"""
Simple JSON-file-backed store for all managed resources.
Each resource type gets its own JSON file under data/.
Provides CRUD operations with Anthropic API-compatible responses.
"""
import json
import os
import time
import uuid
import fcntl

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _store_path(resource_type: str) -> str:
    _ensure_dir()
    return os.path.join(DATA_DIR, f"{resource_type}.json")


def _load(resource_type: str) -> list:
    path = _store_path(resource_type)
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return json.load(f)


def _save(resource_type: str, items: list):
    path = _store_path(resource_type)
    with open(path, "w") as f:
        json.dump(items, f, indent=2)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# ── Generic CRUD ──────────────────────────────────────────────

def create(resource_type: str, id_prefix: str, data: dict, extra_defaults: dict = None) -> dict:
    """Create a new resource. Returns the created object."""
    items = _load(resource_type)
    now = _now()
    obj = {
        "id": _gen_id(id_prefix),
        "type": resource_type,
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        **(extra_defaults or {}),
        **data,
    }
    items.append(obj)
    _save(resource_type, items)
    return obj


def list_all(resource_type: str, limit: int = 20, after_id: str = None, before_id: str = None, include_archived: bool = False) -> dict:
    """List resources with pagination."""
    items = _load(resource_type)
    if not include_archived:
        items = [i for i in items if not i.get("archived_at")]

    # Pagination
    if after_id:
        found = False
        filtered = []
        for item in items:
            if found:
                filtered.append(item)
            if item["id"] == after_id:
                found = True
        items = filtered

    if before_id:
        filtered = []
        for item in items:
            if item["id"] == before_id:
                break
            filtered.append(item)
        items = filtered

    has_more = len(items) > limit
    items = items[:limit]

    return {
        "data": items,
        "has_more": has_more,
        "first_id": items[0]["id"] if items else None,
        "last_id": items[-1]["id"] if items else None,
    }


def get(resource_type: str, resource_id: str) -> dict | None:
    """Get a single resource by ID."""
    items = _load(resource_type)
    for item in items:
        if item["id"] == resource_id:
            return item
    return None


def update(resource_type: str, resource_id: str, updates: dict) -> dict | None:
    """Update a resource by ID. Returns updated object or None."""
    items = _load(resource_type)
    for i, item in enumerate(items):
        if item["id"] == resource_id:
            item.update(updates)
            item["updated_at"] = _now()
            items[i] = item
            _save(resource_type, items)
            return item
    return None


def delete(resource_type: str, resource_id: str) -> bool:
    """Delete a resource by ID. Returns True if found and deleted."""
    items = _load(resource_type)
    new_items = [i for i in items if i["id"] != resource_id]
    if len(new_items) == len(items):
        return False
    _save(resource_type, new_items)
    return True


def archive(resource_type: str, resource_id: str) -> dict | None:
    """Archive a resource. Returns updated object or None."""
    return update(resource_type, resource_id, {"archived_at": _now()})
