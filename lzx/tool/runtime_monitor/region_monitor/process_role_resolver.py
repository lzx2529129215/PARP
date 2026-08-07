from __future__ import annotations

from dataclasses import dataclass


DEFAULT_RULES: dict[str, list[str]] = {
    "WPS_CLOUD_SERVICE": ["wpscloud", "wpscloudsvr", "cloud"],
    "WPS_LIBADAPTER": ["libadapter"],
    "WPS_MAIN": ["wpsoffice", "/wps", "/wpp", "/et", "kingsoft"],
}


@dataclass(frozen=True)
class ProcessRoleResolver:
    rules: dict[str, list[str]]

    @classmethod
    def for_config(cls, config_rules: dict[str, list[str]] | None = None) -> "ProcessRoleResolver":
        merged = {key: list(values) for key, values in DEFAULT_RULES.items()}
        for key, values in (config_rules or {}).items():
            merged[str(key)] = [str(v) for v in values]
        return cls(merged)

    def resolve(self, comm: str, cmdline: str, exe: str) -> str:
        text = " ".join([comm, cmdline, exe]).lower()
        for role, keywords in self.rules.items():
            if any(keyword.lower() in text for keyword in keywords):
                return role
        if "wps" in text or "kingsoft" in text:
            return "WPS_OTHER"
        return "UNKNOWN"

