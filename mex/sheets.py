"""Google Sheets mirror via a service account.

Two ways to feed the spreadsheet are supported; whichever is configured wins,
service account first:

  GOOGLE_SERVICE_ACCOUNT_JSON + GSHEET_SPREADSHEET_ID
      Signs in as a service account and calls the Sheets API directly. No public
      endpoint, and access can be revoked by un-sharing the spreadsheet.
  GSHEET_WEBHOOK_URL
      Apps Script web app. Simpler to set up, no key to protect.

Nothing here ever prints a secret's value. A service-account key pasted into the
wrong place once leaked a private key into this repo's public workflow logs via
a library exception message, so exceptions are reported by type only and the
client_email -- an identifier, not a credential -- is the single field ever
echoed, because the user needs it to share the spreadsheet.
"""
from . import compat  # noqa: F401
import json
import os

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"

_session = None
_checked_tabs: set[str] = set()


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
                and os.environ.get("GSHEET_SPREADSHEET_ID", "").strip())


def _spreadsheet_id() -> str:
    return os.environ.get("GSHEET_SPREADSHEET_ID", "").strip()


def client_email() -> str:
    """The address the spreadsheet must be shared with. Not a credential."""
    try:
        return json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]).get("client_email", "")
    except Exception:  # noqa: BLE001
        return ""


def _get_session():
    global _session
    if _session is not None:
        return _session
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    _session = AuthorizedSession(creds)
    return _session


def _ensure_tab(s, tab: str, header: list[str]) -> None:
    """Create the tab and write its header row once per process."""
    if tab in _checked_tabs:
        return
    sid = _spreadsheet_id()
    meta = s.get(f"{API}/{sid}?fields=sheets.properties.title", timeout=30)
    meta.raise_for_status()
    titles = [sh["properties"]["title"] for sh in meta.json().get("sheets", [])]
    if tab not in titles:
        s.post(f"{API}/{sid}:batchUpdate", timeout=30,
               json={"requests": [{"addSheet": {"properties": {"title": tab}}}]}
               ).raise_for_status()
    first = s.get(f"{API}/{sid}/values/{tab}!1:1", timeout=30)
    first.raise_for_status()
    if not first.json().get("values"):
        s.put(f"{API}/{sid}/values/{tab}!A1?valueInputOption=RAW", timeout=30,
              json={"values": [header]}).raise_for_status()
    _checked_tabs.add(tab)


def append(tab: str, header: list[str], row: dict) -> bool:
    """Append one row, columns ordered by `header`. Never raises."""
    if not configured():
        return False
    try:
        s = _get_session()
        _ensure_tab(s, tab, header)
        values = [["" if row.get(k) is None else row.get(k, "") for k in header]]
        r = s.post(
            f"{API}/{_spreadsheet_id()}/values/{tab}!A1:append"
            "?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
            json={"values": values}, timeout=30)
        if r.status_code >= 400:
            # Status only. Response bodies from Google echo the request URL, and
            # GitHub's masking does not cover a multi-line secret.
            print(f"[sheets] HTTP {r.status_code} saat menulis ke '{tab}'")
            if r.status_code in (403, 404):
                print(f"[sheets] bagikan spreadsheet ke: {client_email()} (akses Editor)")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[sheets] gagal: {type(e).__name__}")
        return False


def reachable() -> bool:
    """Can we read the spreadsheet's metadata? Adds no rows."""
    if not configured():
        return False
    try:
        s = _get_session()
        r = s.get(f"{API}/{_spreadsheet_id()}?fields=spreadsheetId", timeout=30)
        if r.status_code >= 400:
            print(f"[sheets] probe HTTP {r.status_code}; "
                  f"bagikan spreadsheet ke: {client_email()} (akses Editor)")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[sheets] probe gagal: {type(e).__name__}")
        return False
