"""Detection of credential-bearing and otherwise sensitive locations.

This module does not block anything. It classifies, and the permissions engine turns
a classification into a forced confirmation. That split matters: reading your own
browser profile is a legitimate thing to want to do, and the application's job is to
make sure you *knew* that was what you asked for -- not to refuse.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SensitiveKind(StrEnum):
    BROWSER_PROFILE = "browser_profile"
    CREDENTIAL_STORE = "credential_store"
    SSH_KEY = "ssh_key"
    CLOUD_CREDENTIALS = "cloud_credentials"
    PASSWORD_DATABASE = "password_database"  # noqa: S105 - a category name, not a secret
    CRYPTO_WALLET = "crypto_wallet"
    SYSTEM_REGISTRY = "system_registry"
    ENV_SECRETS = "env_secrets"
    SYSTEM_DIRECTORY = "system_directory"


@dataclass(frozen=True, slots=True)
class SensitiveMatch:
    kind: SensitiveKind
    path: Path
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "path": str(self.path), "reason": self.reason}


# Glob patterns matched case-insensitively against the resolved path, using forward
# slashes so one pattern set works regardless of separator. Ordered most-specific first.
_PATTERNS: tuple[tuple[SensitiveKind, str, str], ...] = (
    (
        SensitiveKind.BROWSER_PROFILE,
        "*/appdata/local/google/chrome/user data/*",
        "Chrome profile: cookies, saved passwords and session tokens",
    ),
    (
        SensitiveKind.BROWSER_PROFILE,
        "*/appdata/local/microsoft/edge/user data/*",
        "Edge profile: cookies, saved passwords and session tokens",
    ),
    (
        SensitiveKind.BROWSER_PROFILE,
        "*/appdata/roaming/mozilla/firefox/profiles/*",
        "Firefox profile: cookies, logins.json and key4.db",
    ),
    (
        SensitiveKind.BROWSER_PROFILE,
        "*/appdata/*/brave*/user data/*",
        "Brave profile: cookies and saved passwords",
    ),
    (
        SensitiveKind.CREDENTIAL_STORE,
        "*/appdata/roaming/microsoft/credentials/*",
        "Windows Credential Manager vault",
    ),
    (
        SensitiveKind.CREDENTIAL_STORE,
        "*/appdata/roaming/microsoft/protect/*",
        "DPAPI master keys: decrypt other stored credentials",
    ),
    (SensitiveKind.CREDENTIAL_STORE, "*/appdata/local/microsoft/vault/*", "Windows Vault"),
    (SensitiveKind.SSH_KEY, "*/.ssh/*", "SSH private keys and known_hosts"),
    (SensitiveKind.SSH_KEY, "*/id_rsa*", "SSH private key"),
    (SensitiveKind.SSH_KEY, "*/id_ed25519*", "SSH private key"),
    (SensitiveKind.CLOUD_CREDENTIALS, "*/.aws/credentials*", "AWS access keys"),
    (SensitiveKind.CLOUD_CREDENTIALS, "*/.azure/*", "Azure CLI tokens"),
    (SensitiveKind.CLOUD_CREDENTIALS, "*/gcloud/credentials*", "Google Cloud credentials"),
    (SensitiveKind.CLOUD_CREDENTIALS, "*/.kube/config*", "Kubernetes cluster credentials"),
    (SensitiveKind.CLOUD_CREDENTIALS, "*/.docker/config.json", "Docker registry credentials"),
    (SensitiveKind.CLOUD_CREDENTIALS, "*/.npmrc", "npm auth token"),
    (SensitiveKind.CLOUD_CREDENTIALS, "*/.pypirc", "PyPI upload credentials"),
    (SensitiveKind.CLOUD_CREDENTIALS, "*/.git-credentials", "Stored Git credentials"),
    (SensitiveKind.PASSWORD_DATABASE, "*.kdbx", "KeePass password database"),
    (SensitiveKind.PASSWORD_DATABASE, "*/1password*/*", "1Password data"),
    (SensitiveKind.PASSWORD_DATABASE, "*/bitwarden*/data.json", "Bitwarden vault"),
    (SensitiveKind.PASSWORD_DATABASE, "*/logins.json", "Firefox saved logins"),
    (SensitiveKind.PASSWORD_DATABASE, "*/key4.db", "Firefox key database: decrypts saved logins"),
    (SensitiveKind.PASSWORD_DATABASE, "*/cookies.sqlite", "Browser cookie store: active sessions"),
    (SensitiveKind.PASSWORD_DATABASE, "*/login data", "Chromium saved passwords"),
    (SensitiveKind.CRYPTO_WALLET, "*/wallet.dat", "Cryptocurrency wallet"),
    (SensitiveKind.CRYPTO_WALLET, "*/.ethereum/keystore/*", "Ethereum keystore"),
    (SensitiveKind.CRYPTO_WALLET, "*/appdata/roaming/exodus*/*", "Exodus wallet data"),
    (SensitiveKind.SYSTEM_REGISTRY, "*/windows/system32/config/sam", "Windows account database"),
    (SensitiveKind.SYSTEM_REGISTRY, "*/windows/system32/config/security", "Windows security hive"),
    (SensitiveKind.SYSTEM_REGISTRY, "*/ntuser.dat*", "User registry hive"),
    (SensitiveKind.ENV_SECRETS, "*/.env", "Environment file: commonly holds API keys"),
    (SensitiveKind.ENV_SECRETS, "*/.env.*", "Environment file: commonly holds API keys"),
    (SensitiveKind.ENV_SECRETS, "*secrets.y*ml", "Secrets file"),
    (SensitiveKind.ENV_SECRETS, "*.pem", "Private key or certificate"),
    (SensitiveKind.ENV_SECRETS, "*.pfx", "Private key bundle"),
    (SensitiveKind.ENV_SECRETS, "*.p12", "Private key bundle"),
)


def _system_directories() -> tuple[Path, ...]:
    """Directories where mutation is high-consequence. Resolved from the environment."""
    roots: list[Path] = []
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        if value := os.environ.get(var):
            roots.append(Path(value))
    if not roots and os.name == "nt":
        roots.append(Path("C:/Windows"))
    return tuple(roots)


def classify_path(path: Path, *, mutating: bool = False) -> list[SensitiveMatch]:
    """Return every sensitivity classification that applies to ``path``.

    ``mutating`` adds system-directory matches: reading ``C:\\Windows`` is ordinary,
    while writing to it is not, so the classification depends on the operation.
    """
    text = str(path).replace("\\", "/").lower()
    matches = [
        SensitiveMatch(kind, path, reason)
        for kind, pattern, reason in _PATTERNS
        if fnmatch.fnmatch(text, pattern)
    ]

    if mutating:
        for root in _system_directories():
            root_text = str(root).replace("\\", "/").lower()
            if text == root_text or text.startswith(root_text + "/"):
                matches.append(
                    SensitiveMatch(
                        SensitiveKind.SYSTEM_DIRECTORY,
                        path,
                        f"modifying a protected system location ({root})",
                    )
                )
                break
    return matches


def classify_paths(paths: list[Path], *, mutating: bool = False) -> list[SensitiveMatch]:
    """Classify several paths, preserving order and dropping duplicates."""
    seen: set[tuple[str, str]] = set()
    out: list[SensitiveMatch] = []
    for path in paths:
        for match in classify_path(path, mutating=mutating):
            key = (match.kind.value, str(match.path))
            if key not in seen:
                seen.add(key)
                out.append(match)
    return out
