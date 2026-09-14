#!/usr/bin/env python3
"""Import a list of A-share codes into the Mac 东方财富 watchlist.

Mac East Money has no official file import for 自选股. This script uses the
same cloud watchlist API the official Mac client uses. It reads the current
user's login files from the standard sandbox path under $HOME, and never
hard-codes a username or machine path.

Usage:
    python3 import_watchlist.py --file stocks.txt
    python3 import_watchlist.py --file stocks.txt --apply
"""

from __future__ import annotations

import argparse
import configparser
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

APPKEY = "a3ecb3e4a2e024928bd7687d86058427"  # Mac client's app key, not a user token
MYFAVOR_VER = "5.0"
BASE_URL = "https://myfavor.eastmoney.com/v4/pc"
SYNC_DOWN_URL = f"{BASE_URL}/ggandstkhdfallpt"
SYNC_UP_URL = f"{BASE_URL}/ushdfallpt"
CODE_RE = re.compile(r"^\d{6}$")
LOCK_FILE = Path(os.environ.get("EASTMONEY_IMPORT_LOCK_FILE", "/tmp/eastmoney-mac-import.lock"))

EASTMONEY_CONFIG_DIR = (
    Path.home()
    / "Library/Containers/com.eastmoney.explore/Data/Library/Application Support/eastmoney/config"
)
LOGIN_INI = EASTMONEY_CONFIG_DIR / "LogonLastSuccessInfo.ini"
LOGIN_XML = EASTMONEY_CONFIG_DIR / "LogonUserProfile.xml"
LOG_DIR = (
    Path.home()
    / "Library/Containers/com.eastmoney.explore/Data/Library/Application Support/eastmoney/logs"
)


class EastMoneyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Stock:
    code: str
    name: str = ""
    price: str = ""

    @property
    def eastmoney_code(self) -> str:
        return f"{market_prefix(self.code)}${self.code}"


@dataclass(frozen=True)
class EastMoneySession:
    uid: str
    ut: str
    ct: str
    cid: str
    client_id: str

    def masked(self) -> dict[str, str]:
        return {
            "uid": self.uid,
            "ut": mask_secret(self.ut),
            "ct": mask_secret(self.ct),
            "cid": mask_secret(self.cid),
            "client_id": mask_secret(self.client_id),
        }


def mask_secret(value: str) -> str:
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}...{value[-6:]}"


def market_prefix(code: str) -> str:
    normalized = code.strip()
    if not CODE_RE.fullmatch(normalized):
        raise ValueError(f"invalid stock code: {code}")
    return "1" if normalized.startswith("6") else "0"


def group_name_for(date: datetime) -> str:
    return f"导入{date.month}{date.day}"


def timestamp_for(date: datetime) -> str:
    return date.strftime("%Y%m%d%H%M%S")


def serialize_stock_fields(stocks: list[Stock], date: datetime) -> tuple[str, str, str]:
    scs = ",".join(stock.eastmoney_code for stock in stocks)
    ps = ",".join(stock.price for stock in stocks)
    ts = ",".join(timestamp_for(date) for _ in stocks)
    return scs, ps, ts


def _permission_error(path: Path, label: str, exc: PermissionError) -> EastMoneyError:
    return EastMoneyError(
        f"cannot read {label} {path}. Grant Full Disk Access to Terminal "
        f"(or the app running this script): {exc}"
    )


def _read_text(path: Path, label: str = "file") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except PermissionError as exc:
        raise _permission_error(path, label, exc) from exc


def load_stocks_from_file(path: Path) -> list[Stock]:
    stocks: list[Stock] = []
    seen: set[str] = set()
    text = path.read_text(encoding="utf-8", errors="ignore")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.replace("\t", ",").replace("，", ",")
        parts = [part.strip() for part in line.split(",") if part.strip()]
        if not parts:
            continue
        code = parts[0]
        if code.startswith(("sh", "sz", "bj", "SH", "SZ", "BJ")):
            code = code[2:]
        if not CODE_RE.fullmatch(code) or code in seen:
            continue
        seen.add(code)
        name = parts[1] if len(parts) > 1 else ""
        stocks.append(Stock(code=code, name=name))
    if not stocks:
        raise EastMoneyError(f"no 6-digit stock codes found in {path}")
    return stocks


def load_login_ini() -> dict[str, str]:
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    if not LOGIN_INI.exists():
        return {}
    try:
        parser.read(LOGIN_INI, encoding="utf-8")
    except PermissionError as exc:
        raise _permission_error(LOGIN_INI, "login file", exc) from exc
    if parser.has_section("UserProfile"):
        return {key: value for key, value in parser.items("UserProfile")}
    return {}


def load_login_xml() -> dict[str, str]:
    if not LOGIN_XML.exists():
        return {}
    text = _read_text(LOGIN_XML, "login file")
    values: dict[str, str] = {}
    for tag in ("UID", "UT", "CT", "CID"):
        match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        if match:
            values[tag] = match.group(1).strip()
    return values


def extract_client_id_from_logs() -> str:
    patterns = [
        re.compile(r"clientinfo:\{([0-9a-fA-F-]{36})\}"),
        re.compile(r"clientid=([0-9a-fA-F-]{36})"),
        re.compile(r"userId\{([0-9a-fA-F-]{36})\}"),
    ]
    for path in (LOG_DIR / "SelfStockOper.log", LOG_DIR / "Runtime.log"):
        if not path.exists():
            continue
        try:
            text = _read_text(path, "log file")
        except EastMoneyError:
            continue
        for pattern in patterns:
            matches = pattern.findall(text)
            if matches:
                return matches[-1]
    return str(uuid.uuid4())


def _pick_case_insensitive(values: dict[str, str], name: str) -> str:
    exact = values.get(name)
    if exact:
        return exact
    lower_name = name.lower()
    for key, value in values.items():
        if key.lower() == lower_name and value:
            return value
    return ""


def load_session() -> EastMoneySession:
    env_names = {
        "uid": "EASTMONEY_UID",
        "ut": "EASTMONEY_UT",
        "ct": "EASTMONEY_CT",
        "cid": "EASTMONEY_CID",
        "client_id": "EASTMONEY_CLIENT_ID",
    }
    env_values = {
        name: os.environ.get(env_name, "").strip().strip('"')
        for name, env_name in env_names.items()
    }
    if any(env_values.values()):
        required = ("uid", "ut", "ct", "client_id")
        missing = [env_names[name] for name in required if not env_values[name]]
        if missing:
            raise EastMoneyError(f"missing env login fields: {', '.join(missing)}")
        return EastMoneySession(**env_values)

    ini_values = load_login_ini()
    xml_values = load_login_xml()

    def pick(name: str) -> str:
        return (
            _pick_case_insensitive(ini_values, name)
            or _pick_case_insensitive(xml_values, name)
            or ""
        ).strip().strip('"')

    uid = pick("UID")
    ut = pick("UT")
    ct = pick("CT")
    cid = pick("CID")
    missing = [name for name, value in {"UID": uid, "UT": ut, "CT": ct}.items() if not value]
    if missing:
        raise EastMoneyError(
            "East Money login not found. Open and sign in to the Mac app first, "
            f"then grant Full Disk Access to Terminal. Missing: {', '.join(missing)}"
        )
    return EastMoneySession(
        uid=uid,
        ut=ut,
        ct=ct,
        cid=cid,
        client_id=extract_client_id_from_logs(),
    )


@contextmanager
def import_run_lock():
    try:
        lock_file = LOCK_FILE.open("a+", encoding="utf-8")
    except OSError as exc:
        raise EastMoneyError(f"cannot open lock {LOCK_FILE}: {exc}") from exc
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EastMoneyError("another import is already running") from exc
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


class EastMoneyClient:
    def __init__(self, session: EastMoneySession, timeout: int = 20, use_proxy: bool = True) -> None:
        self.session = session
        self.timeout = timeout
        self.opener = build_opener() if use_proxy else build_opener(ProxyHandler({}))

    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json;chartset=utf-8",
            "ut": self.session.ut,
            "ct": self.session.ct,
            "Referer": "http://www.eastmoney.com",
            "appkey": APPKEY,
            "clientinfo": f"{{{self.session.client_id}}}",
            "myFavorVer": MYFAVOR_VER,
        }

    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=body, headers=self.headers(), method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise EastMoneyError(f"HTTP {exc.code}: {detail[:300]}") from exc
        except URLError as exc:
            raise EastMoneyError(f"network error: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EastMoneyError(f"non-JSON response: {text[:300]}") from exc
        if data.get("state") != 0:
            raise EastMoneyError(f"API error: {data.get('message', data)}")
        return data

    def fetch_groups(self) -> list[dict[str, Any]]:
        data = self.post_json(SYNC_DOWN_URL, {"ggstkreq": []})
        groups = data.get("data", {}).get("grpstkInfos")
        if not isinstance(groups, list):
            raise EastMoneyError(f"cannot read watchlist groups: {data}")
        return groups

    def sync_groups(self, allreq: list[dict[str, str]]) -> dict[str, Any]:
        return self.post_json(SYNC_UP_URL, {"allreq": allreq})


def stock_count_from_group(group: dict[str, Any]) -> int:
    scs = str(group.get("scs") or "")
    if not scs:
        return 0
    return len([part for part in scs.split(",") if part])


def existing_group_request(group: dict[str, Any]) -> dict[str, str]:
    return {
        "gid": str(group.get("gid", "")),
        "name": str(group.get("name", "")),
        "ver": str(group.get("ver", "0")),
        "scs": "",
        "ps": "",
        "ts": "",
        "stknum": "-1",
        "oprflag": "0",
        "source": str(group.get("source", "")),
    }


def new_group_request(name: str, stocks: list[Stock], date: datetime) -> dict[str, str]:
    scs, ps, ts = serialize_stock_fields(stocks, date)
    return {
        "gid": "",
        "name": name,
        "ver": "0",
        "scs": scs,
        "ps": ps,
        "ts": ts,
        "stknum": str(len(stocks)),
        "oprflag": "1",
        "source": "",
    }


def build_final_sync_payload(
    groups: list[dict[str, Any]],
    final_name: str,
    stocks: list[Stock],
    date: datetime,
) -> tuple[list[dict[str, str]], list[str]]:
    removed: list[str] = []
    allreq: list[dict[str, str]] = []
    final_seen = False
    for group in groups:
        name = str(group.get("name", ""))
        if name.startswith("导入临时"):
            removed.append(name)
            continue
        if name == final_name:
            request = new_group_request(final_name, stocks, date)
            request["gid"] = str(group.get("gid", ""))
            request["ver"] = str(group.get("ver", "0"))
            request["source"] = str(group.get("source", ""))
            allreq.append(request)
            final_seen = True
            continue
        allreq.append(existing_group_request(group))
    if not final_seen:
        allreq.append(new_group_request(final_name, stocks, date))
    return allreq, removed


def wait_for_cloud_group(
    client: EastMoneyClient,
    name: str,
    expected_count: int,
    timeout: int,
    poll_interval: float = 3.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_group: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        groups = client.fetch_groups()
        group = next((item for item in groups if str(item.get("name", "")) == name), None)
        if group:
            last_group = group
            if stock_count_from_group(group) == expected_count:
                return group
        time.sleep(poll_interval)
    count = stock_count_from_group(last_group) if last_group else 0
    raise EastMoneyError(f"timed out waiting for group {name}: {count}/{expected_count}")


def detect_eastmoney_app() -> Path | None:
    for name in ("东方财富.app", "东方财富 2.app"):
        path = Path("/Applications") / name
        if path.exists():
            return path
    return None


def relaunch_eastmoney(wait_seconds: float) -> None:
    app = detect_eastmoney_app()
    if app is None:
        print("East Money app not found in /Applications; skip relaunch.", file=sys.stderr)
        return
    subprocess.run(["osascript", "-e", 'tell application "东方财富" to quit'], capture_output=True, timeout=10)
    subprocess.run(["osascript", "-e", 'tell application "东方财富 2" to quit'], capture_output=True, timeout=10)
    time.sleep(1.5)
    subprocess.run(["open", str(app)], check=False)
    time.sleep(wait_seconds)


def print_plan(stocks: list[Stock], final_name: str, session: EastMoneySession | None) -> None:
    print(f"group: {final_name}")
    print(f"stocks: {len(stocks)}")
    for stock in stocks[:8]:
        extra = f" {stock.name}" if stock.name else ""
        print(f"  {stock.code}{extra} -> {stock.eastmoney_code}")
    if len(stocks) > 8:
        print(f"  ... {len(stocks) - 8} more")
    if session:
        print("login: " + json.dumps(session.masked(), ensure_ascii=False))


def apply_import(stocks: list[Stock], args: argparse.Namespace, now: datetime) -> None:
    final_name = args.group_name or group_name_for(now)
    session = load_session()
    client = EastMoneyClient(session=session, timeout=args.timeout, use_proxy=not args.no_proxy)
    print("\n[1/2] reading cloud watchlist...")
    groups = client.fetch_groups()
    allreq, removed = build_final_sync_payload(groups, final_name, stocks, now)
    print_plan(stocks, final_name, session)
    print("\n[2/2] writing cloud group...")
    response = client.sync_groups(allreq)
    allresp = response.get("data", {}).get("allresp", [])
    final_resp = next((item for item in allresp if item.get("name") == final_name), None)
    if not final_resp or final_resp.get("ret") != "1":
        raise EastMoneyError(f"cloud write not confirmed: {response}")
    final_group = wait_for_cloud_group(client, final_name, len(stocks), timeout=args.verify_timeout)
    if removed:
        print(f"removed old temp groups: {removed}")
    print(f"done: {final_group.get('name')} ({stock_count_from_group(final_group)})")
    if args.relaunch:
        print("relaunching East Money so the app pulls the new group...")
        relaunch_eastmoney(args.app_startup_wait)


def run_self_test() -> None:
    date = datetime(2026, 5, 11, 19, 32, 23)
    stocks = [
        Stock(code="600188", name="example-sh", price="21.41"),
        Stock(code="000001", name="example-sz", price="11.20"),
        Stock(code="300001", name="example-cyb", price="18.30"),
    ]
    assert market_prefix("600188") == "1"
    assert market_prefix("000001") == "0"
    scs, ps, ts = serialize_stock_fields(stocks, date)
    assert scs == "1$600188,0$000001,0$300001"
    payload, removed = build_final_sync_payload(
        [
            {"gid": "2", "name": "自选股", "ver": "3", "source": "mac"},
            {"gid": "57", "name": "导入511", "ver": "0", "source": "mac"},
        ],
        "导入511",
        stocks,
        date,
    )
    assert removed == []
    assert payload[1]["gid"] == "57"
    assert payload[1]["stknum"] == "3"
    print("self-test passed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import A-share codes into Mac 东方财富 watchlist (unofficial cloud sync)"
    )
    parser.add_argument("--file", required=False, help="txt/csv of 6-digit codes, one per line")
    parser.add_argument("--apply", action="store_true", help="write to the cloud watchlist")
    parser.add_argument("--self-test", action="store_true", help="run offline unit checks")
    parser.add_argument("--group-name", help="watchlist group name, default 导入{month}{day}")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--verify-timeout", type=int, default=45)
    parser.add_argument("--relaunch", action="store_true", help="quit and reopen East Money after writing")
    parser.add_argument("--app-startup-wait", type=float, default=10.0)
    parser.add_argument("--no-proxy", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
        return 0
    if not args.file:
        parser.error("--file is required unless --self-test")
    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1
    now = datetime.now()
    try:
        stocks = load_stocks_from_file(path)
        if args.apply:
            with import_run_lock():
                apply_import(stocks, args, now)
        else:
            session = None
            try:
                session = load_session()
            except EastMoneyError as exc:
                print(f"login check: {exc}")
            print_plan(stocks, args.group_name or group_name_for(now), session)
            print("\ndry-run only. pass --apply to write. then reopen East Money to sync.")
        return 0
    except (EastMoneyError, FileNotFoundError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
