"""Independent MMO state, credential storage and small shared utilities."""
from __future__ import annotations
import os, re, sys, json, time, base64, hashlib, logging, pathlib, tempfile
from typing import Any, Optional, Dict

APP_NAME = "MetaMovie-Organiser"
APP_DISPLAY_NAME = "MetaMovie Organiser"
APP_VERSION = "1.0.0"
SPLASH_BG = "#0b0f17"
SPLASH_TEXT = "#E6EDF3"
ACCENT = "#48B7FF"

# ------------ Logging ------------
def app_root() -> pathlib.Path:
    """Executable directory for persistent portable state, never _MEIPASS."""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent


def user_state_root() -> pathlib.Path:
    if os.environ.get("MMO_STATE_DIR"):
        return pathlib.Path(os.environ["MMO_STATE_DIR"]).expanduser().resolve()
    if os.name == "nt":
        base = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = pathlib.Path.home() / "Library" / "Application Support"
    else:
        base = pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local" / "share")
    return base / APP_NAME


_PORTABLE_OVERRIDE = None


def portable_mode_enabled() -> bool:
    if _PORTABLE_OVERRIDE is not None:
        return _PORTABLE_OVERRIDE
    if os.environ.get("MMO_PORTABLE") == "1" or (app_root() / "portable.flag").exists():
        return True
    for path in (user_state_root() / "config" / "settings.json", app_root() / "config" / "settings.json"):
        try:
            return bool(json.loads(path.read_text(encoding="utf-8")).get("portable", False))
        except (OSError, ValueError):
            continue
    return False


def state_root(portable: Optional[bool] = None) -> pathlib.Path:
    if os.environ.get("MMO_STATE_DIR"):
        return user_state_root()
    portable = portable_mode_enabled() if portable is None else portable
    return app_root() / "data" if portable else user_state_root()


def check_state_writable(portable: Optional[bool] = None):
    directory = state_root(portable)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=directory) as probe:
        probe.write(b"MMO")
        probe.flush()


def redact_secrets(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?i)Bearer\s+[^\s]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(api[_-]?key|access_token|refresh_token|token|pin|password|authorization)([=:\s]+)[^&\s,}]+", r"\1\2[redacted]", text)
    return text


class SecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        return True


logger = logging.getLogger("MetaMovie")
logger.setLevel(logging.INFO)
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
console.addFilter(SecretFilter())
logger.addHandler(console)
_file_handler = None


def configure_logging():
    global _file_handler
    directory = state_root() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    new = logging.FileHandler(directory / "MMO-Logs.txt", encoding="utf-8")
    new.setFormatter(logging.Formatter("%(asctime)s:%(levelname)s:%(message)s"))
    new.addFilter(SecretFilter())
    if _file_handler:
        logger.removeHandler(_file_handler)
        _file_handler.close()
    _file_handler = new
    logger.addHandler(new)

# ------------ Settings & Credentials ------------
DEFAULT_SETTINGS = {"portable": False, "language": "en", "mode": "rename", "recursive": True,
                    "last_window_size": [1220, 900], "mediainfo": "", "ffprobe": "", "mkvpropedit": ""}


def settings_path(portable: Optional[bool] = None) -> pathlib.Path:
    return state_root(portable) / "config" / "settings.json"


def load_settings() -> Dict[str, Any]:
    out = DEFAULT_SETTINGS.copy()
    for path in (settings_path(), app_root() / "config" / "settings.json"):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                out.update({key: value for key, value in loaded.items() if key in DEFAULT_SETTINGS})
            break
        except (OSError, ValueError):
            continue
    out["portable"] = portable_mode_enabled()
    return out


def save_settings(settings: Dict[str, Any]) -> None:
    settings = {key: value for key, value in settings.items() if key in DEFAULT_SETTINGS}
    atomic_json(settings_path(), settings)
    # Bootstrap choice lives in the user profile, so a saved portable toggle
    # can be discovered before choosing the next run's state directory.
    if portable_mode_enabled():
        bootstrap = user_state_root() / "config" / "settings.json"
        atomic_json(bootstrap, settings)


def atomic_json(path: pathlib.Path, data: Dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = pathlib.Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            json.dump(data, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

class CredentialsStore:
    """Keyring in normal mode, AES-GCM encrypted passphrase vault in portable mode."""
    def __init__(self, portable: bool):
        self.portable = portable
        self.vault_path = state_root(portable) / "vault" / "creds.json.enc"
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        self._passphrase: Optional[str] = None
        self._cached: Dict[str, str] = {}
        self._loaded = False
        self._memory: Dict[str, str] = {}
        self._keyring = None
        if not portable:
            try:
                import keyring
                self._keyring = keyring
            except Exception:
                logger.warning("OS keyring unavailable; credentials will be kept in memory only.")

    def set_session_passphrase(self, pw: Optional[str]):
        if self._passphrase == pw:
            return
        self._passphrase = pw
        self._loaded = False
        self._cached = {}

    def _derive_key(self, salt: bytes, passphrase: str) -> bytes:
        return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)

    def _save_vault(self):
        if not self._passphrase:
            raise RuntimeError("A passphrase is required to save the portable credential vault.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt, nonce = os.urandom(16), os.urandom(12)
        key = self._derive_key(salt, self._passphrase)
        plain = json.dumps(self._cached).encode("utf-8")
        blob = base64.b64encode(b"MMOV1" + salt + nonce + AESGCM(key).encrypt(nonce, plain, b"MMO credentials v1"))
        tmp = self.vault_path.with_suffix(self.vault_path.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.vault_path)
        self._loaded = True

    def _load_vault(self):
        if self._loaded:
            return
        if not self.vault_path.exists():
            self._cached, self._loaded = {}, True
            return
        if not self._passphrase:
            raise RuntimeError("Enter the portable vault passphrase to unlock saved credentials.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.b64decode(self.vault_path.read_bytes())
        if raw[:5] != b"MMOV1" or len(raw) < 34:
            raise RuntimeError("Unsupported or damaged portable credential vault.")
        salt, nonce, ciphertext = raw[5:21], raw[21:33], raw[33:]
        try:
            plain = AESGCM(self._derive_key(salt, self._passphrase)).decrypt(nonce, ciphertext, b"MMO credentials v1")
            self._cached = json.loads(plain.decode("utf-8"))
        except Exception as e:
            raise RuntimeError("Vault passphrase is incorrect or the vault is damaged.") from e
        self._loaded = True

    def get(self, key: str) -> Optional[str]:
        if self.portable:
            self._load_vault()
            return self._cached.get(key)
        if self._keyring:
            try:
                value = self._memory.get(key)
                return value if value is not None else self._keyring.get_password(APP_NAME, key)
            except Exception as e: logger.warning("Keyring read failed: %s", e)
        return self._memory.get(key)

    def set(self, key: str, value: str) -> bool:
        """Return True only when the credential was persisted securely."""
        if self.portable:
            self._load_vault()
            self._cached[key] = value
            self._save_vault()
            return True
        if self._keyring:
            try:
                self._keyring.set_password(APP_NAME, key, value)
                self._memory.pop(key, None)
                return True
            except Exception:
                logger.warning("Keyring write failed; retaining credential for this session only.")
        self._memory[key] = value
        return False


class Cancelled(Exception):
    """Raised at safe cancellation checkpoints."""


def check_cancel(event=None):
    if event is not None and event.is_set():
        raise Cancelled("Cancelled safely.")


def safe_filename(value, limit=180):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', str(value)).strip(' .')
    value = re.sub(r'\s+', ' ', value) or 'Untitled'
    if value.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        value='_'+value
    while len(value.encode('utf-16-le'))//2 > limit:
        value=value[:-1]
    return value.rstrip(' .')
