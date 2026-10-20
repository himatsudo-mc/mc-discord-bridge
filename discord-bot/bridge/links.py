"""Minecraft name (MCID) <-> Discord user links, registered by staff with /link.

Links are keyed by the player's in-game name, case-insensitively, because that
is what the himatsudoLife plugin sends. A Discord user has at most one MCID and
an MCID at most one Discord user; linking either side again replaces the old link.
Bedrock (Floodgate) players must be linked with their in-game name including the
prefix (e.g. ``.Steve``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("bridge.links")

# Java names are 3-16 of [A-Za-z0-9_]; Floodgate adds a prefix such as "." or "*".
MCID_PATTERN = re.compile(r"^[.*]?[A-Za-z0-9_]{1,16}$")


def normalize_mcid(mcid: str) -> str:
    return mcid.strip().lower()


class LinkStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        # normalized mcid -> {"mcid": display name, "discord_id": int, "linked_by": int, "linked_at": float}
        self._by_mcid: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._by_mcid = {
                normalize_mcid(entry["mcid"]): {**entry, "discord_id": int(entry["discord_id"])}
                for entry in raw
            }
        except FileNotFoundError:
            self._by_mcid = {}
        except (ValueError, TypeError, KeyError, OSError):
            # Refuse to start with an unreadable file: continuing empty would wipe it on the next save.
            raise RuntimeError(f"{self._path} could not be read; fix or remove it before starting.") from None

    def _save(self) -> None:
        entries = [{**entry, "discord_id": str(entry["discord_id"])} for entry in self._by_mcid.values()]
        try:
            self._path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Could not write %s.", self._path)

    def link(self, mcid: str, discord_id: int, linked_by: int) -> Optional[str]:
        """Links mcid to discord_id. Returns the MCID previously linked to that user, if it changed."""
        key = normalize_mcid(mcid)
        previous = self.mcid_of(discord_id)
        if previous is not None and normalize_mcid(previous) != key:
            self._by_mcid.pop(normalize_mcid(previous), None)
        else:
            previous = None
        self._by_mcid[key] = {
            "mcid": mcid.strip(),
            "discord_id": discord_id,
            "linked_by": linked_by,
            "linked_at": time.time(),
        }
        self._save()
        return previous

    def unlink_mcid(self, mcid: str) -> Optional[int]:
        entry = self._by_mcid.pop(normalize_mcid(mcid), None)
        if entry is None:
            return None
        self._save()
        return entry["discord_id"]

    def unlink_user(self, discord_id: int) -> Optional[str]:
        mcid = self.mcid_of(discord_id)
        if mcid is None:
            return None
        self.unlink_mcid(mcid)
        return mcid

    def discord_id_of(self, mcid: str) -> Optional[int]:
        entry = self._by_mcid.get(normalize_mcid(mcid))
        return None if entry is None else entry["discord_id"]

    def mcid_of(self, discord_id: int) -> Optional[str]:
        for entry in self._by_mcid.values():
            if entry["discord_id"] == discord_id:
                return entry["mcid"]
        return None
