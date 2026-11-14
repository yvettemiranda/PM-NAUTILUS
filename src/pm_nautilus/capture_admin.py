"""Release server space using byte-for-byte verified local TEST recordings."""

import argparse
import base64
import gzip
import hashlib
import json
from getpass import getpass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .capture_replay import SEGMENT_NAME


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_url(value):
    url = urlsplit(value)
    localhost = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not url.hostname
        or (url.scheme != "https" and not (url.scheme == "http" and localhost))
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError("请使用程序的 HTTPS 地址，例如 https://example.com")
    return urlunsplit((url.scheme, url.netloc, "/api/backtest/capture", "", ""))


def verified_copy(path):
    # Validate the gzip CRC and EOF. Hash the exact compressed bytes for server comparison.
    with gzip.open(path, "rb") as stream:
        while stream.read(1024 * 1024):
            pass
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def release_downloaded(base_url, folder, password, *, opener=None):
    base_url = api_url(base_url)
    opener = opener or build_opener(NoRedirects())
    auth = base64.b64encode(f"pm:{password}".encode()).decode()
    with opener.open(
        Request(base_url + "/archives", headers={"Authorization": f"Basic {auth}"}), timeout=45
    ) as response:
        csrf = response.headers.get("x-pm-csrf")
        archives = json.load(response)["archives"]
    if not csrf:
        raise ValueError("服务器没有返回操作令牌")
    released = 0
    for item in archives:
        name = item["id"]
        if not SEGMENT_NAME.fullmatch(name):
            raise ValueError("服务器返回了无效的分段编号")
        path = folder / name
        if path.is_symlink() or not path.is_file():
            break
        sha256 = verified_copy(path)
        request = Request(
            base_url + "/release",
            data=json.dumps({"id": name, "sha256": sha256}).encode(),
            headers={
                "Authorization": f"Basic {auth}",
                "X-PM-CSRF": csrf,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with opener.open(request, timeout=45) as response:
            csrf = response.headers.get("x-pm-csrf") or csrf
        released += 1
    return released


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="下载的 tar 解压后的录制分段所在目录")
    parser.add_argument("--url", required=True, help="PM-NAUTILUS 的 HTTPS 根地址")
    parser.add_argument(
        "--release", action="store_true", required=True, help="释放已核对的服务器副本"
    )
    args = parser.parse_args(argv)
    try:
        api_url(args.url)
        if not args.directory.is_dir():
            raise ValueError("请选择下载文件解压后的目录")
        count = release_downloaded(args.url, args.directory, getpass("网页登录密码: "))
    except HTTPError as exc:
        parser.exit(2, f"操作中止，服务器返回 HTTP {exc.code}。已核对过的本地副本仍可使用。\n")
    except (OSError, EOFError, ValueError, KeyError) as exc:
        parser.exit(2, f"操作中止：{exc}\n")
    print(f"服务器已释放 {count} 个已核对分段。")
    if not count:
        print("目前服务器没有可释放的分段，或最早分段缺少本地副本。")


if __name__ == "__main__":
    main()
