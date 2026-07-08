"""
文件服务: 上传到 Harness 数据布局的 uploads 目录

物理路径:
    {HARNESS_DATA_ROOT}/users/{user_id}/threads/{thread_id}/user-data/uploads/{filename}

虚拟路径 (agent 可见):
    /mnt/user-data/uploads/{filename}

这样 Harness 的 SandboxProvider 和 UploadsMiddleware 可以直接访问。
"""

import mimetypes
import os
import re
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import UploadFile

from app.config import get_settings
from app.models.file_record import FileRecord

logger = logging.getLogger(__name__)

UPLOAD_MAX_SIZE_MB = int(os.getenv("UPLOAD_MAX_SIZE_MB", "50"))
UPLOAD_MAX_SIZE_BYTES = UPLOAD_MAX_SIZE_MB * 1024 * 1024

ALLOWED_MIME_TYPES = {
    "text/plain", "text/csv", "text/markdown", "text/html",
    "application/json", "application/xml", "application/yaml",
    "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/svg+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/zip", "application/gzip",
}

# 当浏览器返回 application/octet-stream 时, 用扩展名判断真实类型
_ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".html", ".htm", ".xml",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".log",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".css", ".scss", ".sass", ".less", ".sql", ".sh", ".bash", ".zsh",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".php",
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
}

# 文件名安全校验: 禁止路径分隔符、控制字符和遍历模式
_UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\\/:%?*\"<>|]")


def _get_harness_data_root() -> str:
    """Return the Harness data root used by the sandbox provider."""
    return get_settings().harness_data_root


def get_upload_dir(user_id: str, thread_id: str) -> Path:
    """返回当前线程 uploads 目录的物理路径 (Harness 布局)."""
    root = _get_harness_data_root()
    return Path(root) / "users" / user_id / "threads" / thread_id / "user-data" / "uploads"


def _sanitize_filename(name: str | None) -> str:
    """清理文件名, 只保留 basename 并防止路径遍历."""
    if not name:
        return "upload"
    base = Path(name).name
    if not base or base in {".", ".."}:
        return "upload"
    base = base.strip()
    if _UNSAFE_NAME_RE.search(base):
        base = _UNSAFE_NAME_RE.sub("_", base)
    if not base:
        return "upload"
    # 限制长度
    if len(base.encode("utf-8")) > 255:
        stem, suffix = Path(base).stem, Path(base).suffix
        max_stem_bytes = 255 - len(suffix.encode("utf-8"))
        stem_bytes = stem.encode("utf-8")[:max_stem_bytes]
        # 避免截断多字节字符
        while stem_bytes and stem_bytes[-1] & 0xC0 == 0x80:
            stem_bytes = stem_bytes[:-1]
        base = stem_bytes.decode("utf-8", errors="ignore") + suffix
    return base


def _unique_filename(directory: Path, name: str) -> str:
    """若文件名已存在则追加 _1, _2 ..."""
    candidate = name
    counter = 1
    stem, suffix = Path(name).stem, Path(name).suffix
    while (directory / candidate).exists():
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


_TEXT_SNIFF_BYTES = 8192


def _is_known_filename(filename: str | None) -> bool:
    """Recognize special filenames that have no extension but are text files."""
    if not filename:
        return False
    base = Path(filename).name.upper()
    return base in {
        "POSCAR", "CONTCAR", "OUTCAR", "XDATCAR", "KPOINTS",
        "INCAR", "POTCAR", "CHGCAR", "CHGCAR_sum", "AECCAR0",
        "AECCAR1", "AECCAR2", "WAVECAR", "EIGENVAL", "DOSCAR",
        "IBZKPT", "PCDAT", "REPORT", "LOCPOT", "ELFCAR",
        "Makefile", "Dockerfile", "LICENSE", "README", "CHANGELOG",
    }


async def _is_text_content(file: UploadFile) -> bool:
    """Sniff the first few KB and check whether it decodes as UTF-8 text."""
    try:
        sample = await file.read(_TEXT_SNIFF_BYTES)
        # Reset file pointer so save_upload can re-read from the beginning.
        file.file.seek(0)
    except Exception:
        return False

    if not sample:
        return False

    # A small binary file could decode as UTF-8 by accident, so also reject
    # obvious binary markers (null bytes).
    if b"\x00" in sample:
        return False

    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _is_allowed_file(filename: str | None, content_type: str | None) -> bool:
    """Check whether a file is allowed by MIME type and/or extension.

    Browsers often report ``application/octet-stream`` for files they don't
    recognize. In that case we require the filename extension to be in the
    allow-list.
    """
    ext = Path(filename).suffix.lower() if filename else ""

    # Known-good explicit MIME type → allow
    if content_type and content_type in ALLOWED_MIME_TYPES:
        return True

    # Generic/unknown MIME type → require extension allow-list
    if ext in _ALLOWED_EXTENSIONS:
        return True

    # Final fallback: try to guess a specific MIME type from extension
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed and guessed in ALLOWED_MIME_TYPES:
        return True

    return False


async def validate_file(file: UploadFile) -> None:
    """验证文件类型和大小.

    对无扩展名或 MIME 为 application/octet-stream 的文件, 会读取前 8KB
    尝试 UTF-8 解码, 通过则视为文本文件允许上传 (如 POSCAR, Makefile 等).
    """
    filename = file.filename
    content_type = file.content_type

    allowed = _is_allowed_file(filename, content_type)

    # 特殊无扩展名文件 (POSCAR / Makefile 等) 直接放行
    if not allowed and _is_known_filename(filename):
        allowed = True

    # 仍未识别且可能是文本流, 则嗅探内容
    if not allowed and (
        content_type in (None, "application/octet-stream", "text/plain")
        or not Path(filename).suffix
    ):
        allowed = await _is_text_content(file)

    if not allowed:
        raise ValueError(f"不支持的文件类型: {file.content_type}")

    if file.size and file.size > UPLOAD_MAX_SIZE_BYTES:
        raise ValueError(f"文件过大 (最大 {UPLOAD_MAX_SIZE_MB}MB)")


async def save_upload(file: UploadFile, user_id: str, thread_id: str) -> FileRecord:
    """保存上传文件到 Harness uploads 目录并返回元数据记录."""
    upload_dir = get_upload_dir(user_id, thread_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 沙箱内部需要目录可写
    try:
        upload_dir.chmod(0o777)
    except OSError:
        pass

    original_name = _sanitize_filename(file.filename)
    stored_name = _unique_filename(upload_dir, original_name)
    full_path = upload_dir / stored_name

    # 安全写入: 不跟随已有符号链接
    content = await file.read()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(full_path, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        fd = -1
    finally:
        if fd >= 0:
            os.close(fd)

    # storage_path 使用相对于 harness_data_root 的路径, 便于前端/后端复用
    rel_path = str(Path("users") / user_id / "threads" / thread_id / "user-data" / "uploads" / stored_name)

    return FileRecord(
        user_id=user_id,
        thread_id=thread_id,
        filename=stored_name,
        original_name=original_name,
        mime_type=file.content_type or "application/octet-stream",
        size_bytes=len(content),
        storage_path=rel_path,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def upload_virtual_path(filename: str) -> str:
    """Build the sandbox virtual path for a file in uploads."""
    return f"/mnt/user-data/uploads/{filename}"
