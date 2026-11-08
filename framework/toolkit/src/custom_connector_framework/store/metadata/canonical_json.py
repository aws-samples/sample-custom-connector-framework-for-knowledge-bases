"""The one JSON encoding every metadata file uses: sorted keys and no spaces, so equal files hash equally."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict


class CanonicalJsonFile(BaseModel):
    """A metadata file that renders itself by its members' wire names, leaving out members that are None."""

    model_config = ConfigDict(frozen=True)

    def to_json_bytes(self) -> bytes:
        body = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
