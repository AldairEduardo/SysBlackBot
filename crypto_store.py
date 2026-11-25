"""Cifrado local de credenciales.

La contrasena se guarda cifrada con Fernet. La clave de Fernet se guarda
protegida con DPAPI (Windows), de modo que solo el mismo usuario de Windows
en el mismo equipo puede descifrarla. Si DPAPI no esta disponible, se usa
un archivo de clave normal y se avisa por el log.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
import tempfile
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

_ENTROPY = b"blackboard-bot-credentials-v1"
_FLAG_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI solo esta disponible en Windows")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_ubyte)))

    entropy_buf = (ctypes.c_ubyte * len(_ENTROPY)).from_buffer_copy(_ENTROPY)
    entropy_blob = _DataBlob(
        len(_ENTROPY), ctypes.cast(entropy_buf, ctypes.POINTER(ctypes.c_ubyte))
    )

    out_blob = _DataBlob()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _FLAG_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _FLAG_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )

    if not ok:
        raise OSError(ctypes.get_last_error(), "Error de DPAPI")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _protect_document(raw: bytes) -> dict[str, str]:
    try:
        protected = _dpapi(raw, protect=True)
        return {"scheme": "dpapi", "data": base64.b64encode(protected).decode("ascii")}
    except Exception as exc:  # pragma: no cover - depende de Windows
        log.warning("DPAPI no disponible (%s); la clave quedara sin proteger.", exc)
        return {"scheme": "plain", "data": base64.b64encode(raw).decode("ascii")}


def _unprotect_document(doc: dict) -> bytes:
    raw = base64.b64decode(doc["data"])
    if doc.get("scheme") == "dpapi":
        return _dpapi(raw, protect=False)
    return raw


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


@lru_cache(maxsize=8)
def _fernet_for(key_path: str) -> Fernet:
    path = Path(key_path)
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            key = _unprotect_document(doc)
        except Exception as exc:
            raise RuntimeError(
                f"No se pudo leer la clave de cifrado ({path}). "
                "Si cambiaste de usuario/equipo, borra la carpeta de datos y vuelve a /login."
            ) from exc
    else:
        key = Fernet.generate_key()
        doc = _protect_document(key)
        _atomic_write(path, json.dumps(doc).encode("utf-8"))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    try:
        return Fernet(key)
    except Exception as exc:
        raise RuntimeError("La clave de cifrado guardada no es valida.") from exc


def encrypt_json(key_path: Path | str, payload: dict) -> bytes:
    fernet = _fernet_for(str(key_path))
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return fernet.encrypt(raw)


def decrypt_json(key_path: Path | str, blob: bytes) -> dict:
    fernet = _fernet_for(str(key_path))
    try:
        raw = fernet.decrypt(blob)
    except InvalidToken as exc:
        raise RuntimeError(
            "No pude descifrar la contrasena guardada. Vuelve a iniciar sesion con /login."
        ) from exc
    return json.loads(raw.decode("utf-8"))
