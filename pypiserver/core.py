#! /usr/bin/env python
"""minimal PyPI like server for use with pip/easy_install"""

import hashlib
import logging
import mimetypes
import os
import re
import typing as t
from urllib.parse import quote


log = logging.getLogger(__name__)


mimetypes.add_type("application/octet-stream", ".egg")
mimetypes.add_type("application/octet-stream", ".whl")
mimetypes.add_type("text/plain", ".asc")


# ### Next 2 functions adapted from :mod:`distribute.pkg_resources`.
#
component_re = re.compile(r"(\d+ | [a-z]+ | \.| -)", re.I | re.VERBOSE)
replace = {"pre": "c", "preview": "c", "-": "final-", "rc": "c", "dev": "@"}.get


def _parse_version_parts(s):
    for part in component_re.split(s):
        part = replace(part, part)
        if part in ["", "."]:
            continue
        if part[:1] in "0123456789":
            yield part.zfill(8)  # pad for numeric comparison
        else:
            yield "*" + part

    yield "*final"  # ensure that alpha/beta/candidate are before final


def parse_version(s):
    parts = []
    for part in _parse_version_parts(s.lower()):
        if part.startswith("*"):
            # remove trailing zeros from each series of numeric parts
            while parts and parts[-1] == "00000000":
                parts.pop()
        parts.append(part)
    return tuple(parts)


#
#### -- End of distribute's code.


_archive_suffix_rx = re.compile(
    r"(\.zip|\.tar\.gz|\.tgz|\.tar\.bz2|-py[23]\.\d-.*|"
    r"\.win-amd64-py[23]\.\d\..*|\.win32-py[23]\.\d\..*|\.egg)$",
    re.I,
)
wheel_file_re = re.compile(
    r"""^(?P<namever>(?P<name>.+?)-(?P<ver>\d.*?))
    ((-(?P<build>\d.*?))?-(?P<pyver>.+?)-(?P<abi>.+?)-(?P<plat>.+?)
    \.whl|\.dist-info)$""",
    re.VERBOSE,
)
_pkgname_re = re.compile(r"-\d+[a-z_.!+]", re.I)
_pkgname_parts_re = re.compile(
    r"[\.\-](?=cp\d|py\d|macosx|linux|sunos|solaris|irix|aix|cygwin|win)", re.I
)


def _guess_pkgname_and_version_wheel(basename):
    m = wheel_file_re.match(basename)
    if not m:
        return None, None
    name = m.group("name")
    ver = m.group("ver")
    build = m.group("build")
    if build:
        return name, ver + "-" + build
    else:
        return name, ver


def guess_pkgname_and_version(path):
    path = os.path.basename(path)
    if path.endswith(".asc"):
        path = path.rstrip(".asc")
    if path.endswith(".whl"):
        return _guess_pkgname_and_version_wheel(path)
    if not _archive_suffix_rx.search(path):
        return
    path = _archive_suffix_rx.sub("", path)
    if "-" not in path:
        pkgname, version = path, ""
    elif path.count("-") == 1:
        pkgname, version = path.split("-", 1)
    elif "." not in path:
        pkgname, version = path.rsplit("-", 1)
    else:
        pkgname = _pkgname_re.split(path)[0]
        ver_spec = path[len(pkgname) + 1 :]
        parts = _pkgname_parts_re.split(ver_spec)
        version = parts[0]
    return pkgname, version


def normalize_pkgname(name):
    """Perform PEP 503 normalization"""
    return re.sub(r"[-_.]+", "-", name).lower()


def normalize_pkgname_for_url(name):
    """Perform PEP 503 normalization and ensure the value is safe for URLs."""
    return quote(re.sub(r"[-_.]+", "-", name).lower())


def is_allowed_path(path_part):
    p = path_part.replace("\\", "/")
    return not (p.startswith(".") or "/." in p)


class PkgFile:

    __slots__ = [
        "fn",
        "root",
        "_fname_and_hash",
        "relfn",
        "relfn_unix",
        "pkgname_norm",
        "pkgname",
        "version",
        "parsed_version",
        "replaces",
    ]

    def __init__(
        self, pkgname, version, fn=None, root=None, relfn=None, replaces=None
    ):
        self.pkgname = pkgname
        self.pkgname_norm = normalize_pkgname(pkgname)
        self.version = version
        self.parsed_version = parse_version(version)
        self.fn = fn
        self.root = root
        self.relfn = relfn
        self.relfn_unix = None if relfn is None else relfn.replace("\\", "/")
        self.replaces = replaces

    def __repr__(self):
        return "{}({})".format(
            self.__class__.__name__,
            ", ".join(
                [
                    f"{k}={getattr(self, k, 'AttributeError')!r}"
                    for k in sorted(self.__slots__)
                ]
            ),
        )

    def fname_and_hash(self, hash_algo):
        if not hasattr(self, "_fname_and_hash"):
            if hash_algo:
                self._fname_and_hash = (
                    f"{self.relfn_unix}#{hash_algo}="
                    f"{digest_file(self.fn, hash_algo)}"
                )
            else:
                self._fname_and_hash = self.relfn_unix
        return self._fname_and_hash


def _listdir(root: str) -> t.Iterable[PkgFile]:
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [x for x in dirnames if is_allowed_path(x)]
        for x in filenames:
            fn = os.path.join(root, dirpath, x)
            if not is_allowed_path(x) or not os.path.isfile(fn):
                continue
            res = guess_pkgname_and_version(x)
            if not res:
                # #Seems the current file isn't a proper package
                continue
            pkgname, version = res
            if pkgname:
                yield PkgFile(
                    pkgname=pkgname,
                    version=version,
                    fn=fn,
                    root=root,
                    relfn=fn[len(root) + 1 :],
                )


def find_packages(pkgs, prefix=""):
    prefix = normalize_pkgname(prefix)
    for x in pkgs:
        if prefix and x.pkgname_norm != prefix:
            continue
        yield x


def get_prefixes(pkgs):
    normalized_pkgnames = set()
    for x in pkgs:
        if x.pkgname:
            normalized_pkgnames.add(x.pkgname_norm)
    return normalized_pkgnames


def exists(root, filename):
    assert "/" not in filename
    dest_fn = os.path.join(root, filename)
    return os.path.exists(dest_fn)


def store(root, filename, save_method):
    assert "/" not in filename
    dest_fn = os.path.join(root, filename)
    save_method(dest_fn, overwrite=True)  # Overwite check earlier.


def get_bad_url_redirect_path(request, prefix):
    """Get the path for a bad root url."""
    p = request.custom_fullpath
    if p.endswith("/"):
        p = p[:-1]
    p = p.rsplit("/", 1)[0]
    prefix = quote(prefix)
    p += "/simple/{}/".format(prefix)
    return p


def _digest_file(fpath, hash_algo):
    """
    Reads and digests a file according to specified hashing-algorith.

    :param str sha256: any algo contained in :mod:`hashlib`
    :return: <hash_algo>=<hex_digest>

    From http://stackoverflow.com/a/21565932/548792
    """
    blocksize = 2 ** 16
    digester = hashlib.new(hash_algo)
    with open(fpath, "rb") as f:
        for block in iter(lambda: f.read(blocksize), b""):
            digester.update(block)
    return digester.hexdigest()


try:
    from .cache import cache_manager

    def listdir(root: str) -> t.Iterable[PkgFile]:
        # root must be absolute path
        return cache_manager.listdir(root, _listdir)

    def digest_file(fpath, hash_algo):
        # fpath must be absolute path
        return cache_manager.digest_file(fpath, hash_algo, _digest_file)


except ImportError:
    listdir = _listdir
    digest_file = _digest_file