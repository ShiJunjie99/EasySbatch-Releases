"""Exact software identities only. No LLM, filesystem lookup or fuzzy matching."""

from typing import Literal
from pydantic import Field

from .models import _Model
from .server_catalog import ServerCatalog, SoftwareCatalogEntry


class SoftwareResolution(_Model):
    status: Literal["MATCHED", "MULTIPLE", "NO_MATCH", "UNRESOLVED"]
    choices: list[SoftwareCatalogEntry] = Field(default_factory=list)
    reason: str


class SoftwareResolver:
    def resolve(self, requirement: str | None, catalog: ServerCatalog) -> SoftwareResolution:
        if not requirement:
            return SoftwareResolution(status="UNRESOLVED", reason="没有明确软件名称，不猜软件或路径。")
        name = requirement.casefold()
        matches = [s for s in catalog.software if name in
                   {s.id.casefold(), s.display_name.casefold(), *(a.casefold() for a in s.aliases)}]
        matches.sort(key=lambda s: s.id)
        status = "MATCHED" if len(matches) == 1 else "MULTIPLE" if matches else "NO_MATCH"
        return SoftwareResolution(status=status, choices=matches, reason={
            "MATCHED": "唯一 Server catalog 软件名称匹配；验证范围见条目，不代表计算已通过。",
            "MULTIPLE": "多个软件版本匹配，请确认；不自动选择最新版本。",
            "NO_MATCH": "Server catalog 没有精确匹配；不猜绝对路径、不安装软件。",
        }[status])
