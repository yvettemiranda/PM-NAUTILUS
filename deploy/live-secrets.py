#!/usr/bin/env python3
"""Create an age-encrypted LIVE credential bundle and unlock it into /run.

Secrets are entered at a terminal, never as command arguments or shell variables.
The decrypted file is created only after age authentication and JSON validation.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import warnings
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_VAULT = Path("/etc/pm-nautilus/live.json.age")
RUNTIME_DIR = Path("/run/pm-nautilus")
RUNTIME_FILE = RUNTIME_DIR / "live.json"
APP_UID = 10001
MAX_CIPHERTEXT = 1_000_000
MAX_PLAINTEXT = 64_000
SDK_VERSION = "1.0.1"


class SetupError(Exception):
    pass


def require_terminal():
    try:
        with open("/dev/tty", "rb"):
            pass
    except OSError as exc:
        raise SetupError("需要交互式终端；不要通过命令行参数或管道提供密钥") from exc


def age_binary():
    binary = shutil.which("age")
    if binary is None:
        raise SetupError("未找到 age；请先安装 age 加密工具")
    return binary


def check_private_directory(path: Path, *, create: bool):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise SetupError("凭据目录必须是当前管理员拥有的普通目录")
    if info.st_mode & 0o077:
        raise SetupError("凭据目录不能允许组或其他用户访问；应为 0700")


def validate_credentials(data):
    required = {
        "private_key",
        "api_key",
        "api_secret",
        "passphrase",
        "funder",
        "signature_type",
        "rpc_url",
        "auto_approve_redemption",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise SetupError("凭据 JSON 字段不完整或含未知字段")
    if not isinstance(data["private_key"], str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{64}", data["private_key"]
    ):
        raise SetupError("private_key 格式不正确")
    if not isinstance(data["funder"], str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{40}", data["funder"]
    ):
        raise SetupError("funder 地址格式不正确")
    if type(data["signature_type"]) is not int or data["signature_type"] not in (0, 2):
        raise SetupError("signature_type 必须为 0 或 2")
    for key in ("api_key", "api_secret", "passphrase"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise SetupError(f"{key} 不能为空")
    url = data["rpc_url"]
    if not isinstance(url, str) or urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
        raise SetupError("rpc_url 必须是 HTTPS URL")
    if type(data["auto_approve_redemption"]) is not bool:
        raise SetupError("auto_approve_redemption 必须为布尔值")
    return data


def derive_api_credentials(private_key, funder, signature_type, client_class=None):
    """Use the pinned official SDK for L1 auth; never print its response/errors."""
    if client_class is None:
        try:
            if metadata.version("py-clob-client-v2") != SDK_VERSION:
                raise SetupError(f"仅允许已验证的 py-clob-client-v2=={SDK_VERSION}")
            from py_clob_client_v2.client import ClobClient
        except metadata.PackageNotFoundError as exc:
            raise SetupError("未安装锁定的 Polymarket SDK；请先安装项目依赖") from exc
        client_class = ClobClient
    try:
        client = client_class(
            "https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            signature_type=signature_type,
            funder=funder,
        )
        creds = client.create_or_derive_api_key()
        return {
            "api_key": creds.api_key,
            "api_secret": creds.api_secret,
            "passphrase": creds.api_passphrase,
        }
    except Exception:
        # Third-party exceptions can contain HTTP headers or request details.
        raise SetupError("CLOB L2 凭据创建或派生失败；未写入凭据文件") from None


def read_interactive_credentials(*, derive):
    # getpass otherwise falls back to visible input when echo cannot be disabled.
    warnings.filterwarnings("error", category=getpass.GetPassWarning)
    print("逐项输入 LIVE 凭据；输入内容不会回显，也不会写入明文磁盘。", file=sys.stderr)
    private_key = getpass.getpass("签名钱包 private_key (0x...): ")
    if private_key != getpass.getpass("再次输入 private_key: "):
        raise SetupError("两次 private_key 输入不一致")
    data = {
        "private_key": private_key,
        "funder": getpass.getpass("资金钱包 funder (0x...): "),
        "signature_type": int(getpass.getpass("signature_type (0 或 2): ")),
        "rpc_url": getpass.getpass("Polygon HTTPS rpc_url: "),
        "auto_approve_redemption": False,
    }
    # Validate the identity before any authenticated SDK request. Temporary
    # placeholders satisfy only the non-empty API-field checks.
    validate_credentials(
        data | {"api_key": "pending", "api_secret": "pending", "passphrase": "pending"}
    )
    if derive:
        print("正在用官方 SDK 创建或派生 CLOB L2 凭据；不会下单。", file=sys.stderr)
        data.update(
            derive_api_credentials(data["private_key"], data["funder"], data["signature_type"])
        )
    else:
        data.update(
            api_key=getpass.getpass("CLOB api_key: "),
            api_secret=getpass.getpass("CLOB api_secret: "),
            passphrase=getpass.getpass("CLOB passphrase: "),
        )
    return validate_credentials(data)


def run_age(binary: str, args: list[str], data: bytes):
    # age reads its passphrase from /dev/tty. The secret payload uses stdin/stdout
    # pipes, not argv, environment variables, or a temporary plaintext file.
    result = subprocess.run(
        [binary, *args],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        raise SetupError("age 加密或解锁失败；未写入新凭据文件")
    return result.stdout


def write_new_file(path: Path, payload: bytes, uid: int, gid: int):
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(fd, "wb") as handle:
            os.fchown(handle.fileno(), uid, gid)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # link is atomic and fails if a prior vault/unlock exists. A failed run
        # cannot replace a working credential file.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def create(vault: Path, *, derive: bool):
    require_terminal()
    binary = age_binary()
    check_private_directory(vault.parent, create=True)
    if vault.exists() or vault.is_symlink():
        raise SetupError("加密凭据已存在；请先安全备份并明确处理旧版本")
    data = read_interactive_credentials(derive=derive)
    plaintext = (json.dumps(data, separators=(",", ":")) + "\n").encode()
    ciphertext = run_age(binary, ["-p"], plaintext)
    if not ciphertext or len(ciphertext) > MAX_CIPHERTEXT:
        raise SetupError("加密凭据大小异常")
    write_new_file(vault, ciphertext, os.geteuid(), os.getegid())
    print(f"已创建加密凭据：{vault}（未写入明文文件）")


def check_tmpfs():
    result = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "--target", "/run"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode or result.stdout.strip() != "tmpfs":
        raise SetupError("/run 不是 tmpfs；拒绝在磁盘上解锁凭据")


def unlock(vault: Path):
    if os.geteuid() != 0:
        raise SetupError("解锁须由 root 在服务器终端执行，以建立受控 /run 文件")
    require_terminal()
    binary = age_binary()
    check_tmpfs()
    check_private_directory(vault.parent, create=False)
    info = vault.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise SetupError("加密凭据必须是 root 所有、组及其他用户不可读的普通文件")
    if info.st_size > MAX_CIPHERTEXT:
        raise SetupError("加密凭据大小异常")
    if RUNTIME_DIR.is_symlink():
        raise SetupError("/run 凭据目录不能是符号链接")
    check_private_directory(RUNTIME_DIR, create=True)
    if RUNTIME_FILE.exists() or RUNTIME_FILE.is_symlink():
        raise SetupError("/run 凭据已存在；先停容器并明确处理当前解锁状态")
    # Read the ciphertext through a checked, no-follow descriptor so a symlink
    # or path swap cannot redirect the file between validation and decryption.
    fd = os.open(vault, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0 or opened.st_mode & 0o077:
            raise SetupError("加密凭据在读取时已改变")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            ciphertext = handle.read(MAX_CIPHERTEXT + 1)
    finally:
        os.close(fd)
    if len(ciphertext) > MAX_CIPHERTEXT:
        raise SetupError("加密凭据大小异常")
    plaintext = run_age(binary, ["-d"], ciphertext)
    if len(plaintext) > MAX_PLAINTEXT:
        raise SetupError("解锁结果大小异常")
    try:
        validate_credentials(json.loads(plaintext))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError("解锁内容不是有效凭据 JSON") from exc
    write_new_file(RUNTIME_FILE, plaintext, APP_UID, APP_UID)
    print("凭据已解锁到 /run/pm-nautilus/live.json；请按部署文档检查权限后启动 LIVE。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "unlock"))
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument(
        "--derive-api-credentials",
        action="store_true",
        help="创建时用锁定的官方 SDK 派生 CLOB L2 凭据，不在终端显示其值",
    )
    args = parser.parse_args()
    if args.action == "unlock" and args.derive_api_credentials:
        parser.error("--derive-api-credentials 仅用于 create")
    try:
        if args.action == "create":
            create(args.vault, derive=args.derive_api_credentials)
        else:
            unlock(args.vault)
    except (SetupError, OSError, ValueError, getpass.GetPassWarning) as exc:
        # Never print third-party exception messages: paths and RPC URLs can be
        # secret-bearing, and this tool is intended for an admin terminal.
        if isinstance(exc, SetupError):
            print(f"错误：{exc}", file=sys.stderr)
        else:
            print("错误：凭据操作失败；未输出任何密钥内容", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
