import os
import json
import asyncio
import shutil
import nest_asyncio
import pandas as pd
from getpass import getpass
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
from playwright.async_api import async_playwright
from datetime import datetime, timezone

try:
    import gradio as gr
except ImportError:
    gr = None

nest_asyncio.apply()

BAND_DETAIL_URL = "https://api.bandwith.info/bands/show"
BAND_INDEX_URL = "https://api.bandwith.info/bands/index"
AUTH_SIGN_IN_URL = "https://api.bandwith.info/auth/sign_in"

BASE_OUT_ROOT = Path(os.environ.get("BANDWITH_OUTPUT_DIR", "content"))

DEBUG_ENABLED = False
DEBUG_TARGET_BANDS = {"20217363-c174-40d7-9522-cb91ccdb2f94"}
DEBUG_ALL_BANDS = False
TOKEN_HEADER_KEYS = ["access-token", "client", "uid", "token-type", "expiry", "bandwith-version"]
INVALID_PATH_CHARS = set(r'\/:*?"<>|')


def debug_log(band_id: Optional[str], message: str) -> None:
    if not DEBUG_ENABLED:
        return
    target = band_id or "-"
    if DEBUG_ALL_BANDS or not DEBUG_TARGET_BANDS or target in DEBUG_TARGET_BANDS:
        print(f"[DEBUG][{target}] {message}")


def to_int_or_none(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def normalize_members(obj) -> List[str]:
    names: List[str] = []
    if isinstance(obj, list):
        for member in obj:
            if not isinstance(member, dict):
                continue
            for key in [
                "name",
                "display_name",
                "full_name",
                "nickname",
                "member_name",
                "user_name",
            ]:
                value = member.get(key)
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
                    break
    return list(dict.fromkeys(names))


def extract_member_names(detail: Dict) -> List[str]:
    names: List[str] = []
    band_id = detail.get("id") if isinstance(detail, dict) else None
    if band_id:
        debug_log(band_id, f"extract_member_names: keys={list(detail.keys())}")

    user_bands = detail.get("user_bands") if isinstance(detail, dict) else None
    if isinstance(user_bands, list):
        debug_log(band_id, f"extract_member_names: user_bands count={len(user_bands)}")
        for entry in user_bands:
            if not isinstance(entry, dict):
                continue
            display = None
            user = entry.get("user") or {}
            if isinstance(user, dict):
                for key in ("nickname", "name", "custom_id"):
                    value = user.get(key)
                    if isinstance(value, str) and value.strip():
                        display = value.strip()
                        break
            member_name = entry.get("member_name")
            if not display and isinstance(member_name, str) and member_name.strip():
                display = member_name.strip()
            if display:
                names.append(display)

    if not names and isinstance(detail, dict):
        members = detail.get("members")
        if isinstance(members, list):
            names = normalize_members(members)
            debug_log(band_id, f"extract_member_names: fallback members list -> {names}")

    if not names and isinstance(detail, dict):
        data_members = detail.get("data")
        if isinstance(data_members, list):
            names = normalize_members(data_members)
            debug_log(band_id, f"extract_member_names: fallback data list -> {names}")

    deduped = list(dict.fromkeys(names))
    debug_log(band_id, f"extract_member_names: deduped={deduped}")
    return deduped


def summarize_musics(band: Dict) -> str:
    entries = []
    for item in band.get("musics") or []:
        if not isinstance(item, dict):
            continue
        order = to_int_or_none(item.get("order"))
        duration = to_int_or_none(item.get("time"))
        name = (item.get("name") or "").strip()
        if not name:
            continue
        order_prefix = f"{order}:" if order is not None else ""
        duration_suffix = f"({duration}m)" if duration is not None else ""
        label = f"{order_prefix}{name}{duration_suffix}".strip()
        entries.append((999999 if order is None else order, label))
    entries.sort(key=lambda x: (x[0], x[1]))
    return " / ".join(label for _, label in entries if label)


async def collect_raw_data(email: str, password: str, exclude_nickname: str) -> Tuple[Path, List[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, int]]]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        try:
            auth_headers = await api_sign_in(context, email, password)
            bands_data = await fetch_band_index(context, auth_headers)

            safe_nickname = exclude_nickname.strip() or "default"
            sanitized = "".join("_" if c in INVALID_PATH_CHARS else c for c in safe_nickname)
            if not sanitized:
                sanitized = "default"
            out_dir = BASE_OUT_ROOT / sanitized

            nickname_cf = exclude_nickname.casefold()
            rows: List[Dict[str, Any]] = []
            member_counts: Dict[str, int] = {}
            band_stats: Dict[str, Dict[str, int]] = {}

            for band in bands_data:
                if not isinstance(band, dict):
                    continue
                band_name = (band.get("original_name") or band.get("name") or "").strip()
                raw_members = await try_fetch_members(context, band, auth_headers)
                members = [
                    m.strip()
                    for m in raw_members
                    if isinstance(m, str) and m.strip() and m.strip().casefold() != nickname_cf
                ]
                songs = summarize_musics(band)
                song_count = sum(
                    1
                    for item in (band.get("musics") or [])
                    if isinstance(item, dict) and (item.get("name") or "").strip()
                )

                row = {
                    "band_name": band_name,
                    "members": " / ".join(members),
                    "member_list": members,
                    "songs": songs,
                    "song_count": song_count,
                }
                rows.append(row)

                for member in row.get("member_list") or []:
                    member_counts[member] = member_counts.get(member, 0) + 1

                if band_name:
                    stats = band_stats.setdefault(band_name, {"count": 0, "song_count": 0})
                    stats["count"] += 1
                    stats["song_count"] += song_count

            return out_dir, rows, member_counts, band_stats
        finally:
            await context.close()
            await browser.close()


def build_json_payload(
    out_dir: Path,
    rows: List[Dict[str, Any]],
    member_counts: Dict[str, int],
    band_stats: Dict[str, Dict[str, int]],
    exclude_nickname: str,
) -> Dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    bands_payload = [
        {
            "bandName": row["band_name"],
            "members": row["member_list"],
            "memberSummary": row["members"],
            "songs": row["songs"],
            "songCount": row["song_count"],
        }
        for row in rows
    ]
    member_rank_payload = [
        {"memberName": name, "appearanceCount": count}
        for name, count in sorted(member_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    band_rank_payload = [
        {
            "bandName": name,
            "appearanceCount": stats["count"],
            "songCount": stats["song_count"],
        }
        for name, stats in sorted(band_stats.items(), key=lambda item: (-item[1]["count"], item[0]))
    ]
    return {
        "bands": bands_payload,
        "memberRank": member_rank_payload,
        "bandRank": band_rank_payload,
        "meta": {
            "generatedAt": generated_at,
            "bandCount": len(bands_payload),
            "excludedNickname": exclude_nickname,
            "outputDirectory": str(out_dir),
        },
    }


async def scrape_as_json(email: str, password: str, exclude_nickname: str) -> Dict[str, Any]:
    out_dir, rows, member_counts, band_stats = await collect_raw_data(email, password, exclude_nickname)
    return build_json_payload(out_dir, rows, member_counts, band_stats, exclude_nickname)


def update_tokens(auth_headers: Dict[str, str], resp) -> None:
    for key in TOKEN_HEADER_KEYS:
        value = resp.headers.get(key)
        if value:
            auth_headers[key] = value
    auth_headers.setdefault("token-type", "Bearer")
    auth_headers.setdefault("bandwith-version", "3")


async def api_sign_in(context, email: str, password: str) -> Dict[str, str]:
    payload = json.dumps({"email": email, "password": password})
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://www.bandwith.info",
        "Referer": "https://www.bandwith.info/",
    }
    resp = await context.request.post(
        AUTH_SIGN_IN_URL,
        data=payload,
        headers=headers,
        timeout=20000,
    )
    if not resp.ok:
        raise RuntimeError(f"API sign-in failed with status {resp.status}")

    auth_headers: Dict[str, str] = {}
    update_tokens(auth_headers, resp)
    return auth_headers


async def fetch_band_index(context, auth_headers: Dict[str, str]) -> List[Dict]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://www.bandwith.info",
        "Referer": "https://www.bandwith.info/",
    }
    headers.update(auth_headers)

    resp = await context.request.post(
        BAND_INDEX_URL,
        data=json.dumps({}),
        headers=headers,
        timeout=20000,
    )
    if not resp.ok:
        raise RuntimeError(f"Failed to fetch band index: {resp.status}")

    update_tokens(auth_headers, resp)
    payload = await resp.json()
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected response for band index")
    return payload


async def fetch_band_detail(
    context,
    band_id: str,
    auth_headers: Dict[str, str],
    circle_id: Optional[str] = None,
) -> Optional[Dict]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://www.bandwith.info",
        "Referer": "https://www.bandwith.info/",
    }
    headers.update(auth_headers)
    if circle_id:
        headers["circle-id"] = circle_id

    resp = await context.request.post(
        BAND_DETAIL_URL,
        data=json.dumps({"band_id": band_id}),
        headers=headers,
        timeout=20000,
    )
    if not resp.ok:
        debug_log(band_id, f"fetch_band_detail: status={resp.status}")
        return None

    update_tokens(auth_headers, resp)
    ctype = resp.headers.get("content-type", "")
    if "application/json" not in ctype:
        debug_log(band_id, f"fetch_band_detail: unexpected content-type {ctype}")
        return None

    try:
        detail = await resp.json()
    except Exception as exc:
        debug_log(band_id, f"fetch_band_detail: json error -> {exc}")
        return None
    return detail if isinstance(detail, dict) else None


async def try_fetch_members(context, band: Dict, auth_headers: Dict[str, str]) -> List[str]:
    band_id = band.get("id")
    circle_id = band.get("circle_id")
    debug_log(band_id, f"try_fetch_members: start circle_id={circle_id}")
    if not band_id:
        return []

    detail = await fetch_band_detail(context, band_id, auth_headers, circle_id)
    if detail:
        names = extract_member_names(detail)
        if names:
            debug_log(band_id, f"try_fetch_members: names from detail={names}")
            return names

    debug_log(band_id, "try_fetch_members: no members extracted")
    return []


def prompt_credentials() -> Tuple[str, str, str]:
    env_email = (os.environ.get("BANDWITH_EMAIL") or "").strip()
    env_password = os.environ.get("BANDWITH_PASSWORD") or ""
    env_nickname = (os.environ.get("BANDWITH_NICKNAME") or "").strip()

    if env_email and env_password and env_nickname:
        return env_email, env_password, env_nickname

    email = input("Email: " if not env_email else f"Email [{env_email}]: " ).strip()
    password = getpass("Password: " if not env_password else "Password [env]: ")
    nickname = input("Nickname to exclude: " if not env_nickname else f"Nickname to exclude [{env_nickname}]: " ).strip()

    if not email:
        email = env_email
    if not password:
        password = env_password
    if not nickname:
        nickname = env_nickname

    if not email or not password or not nickname:
        raise RuntimeError("Email, password, and nickname are required to log in.")

    return email.strip(), password, nickname.strip()


async def main(email: str, password: str, exclude_nickname: str, *, write_csv: bool = True) -> Tuple[Path, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    out_dir, rows, member_counts, band_stats = await collect_raw_data(email, password, exclude_nickname)

    out_bands_path = out_dir / "bands.csv"
    out_band_rank_path = out_dir / "band_rank.csv"
    out_member_rank_path = out_dir / "memb_rank.csv"

    df_bands = pd.DataFrame(
        [
            {
                "band_name": row["band_name"],
                "members": row["members"],
                "songs": row["songs"],
            }
            for row in rows
        ]
    )

    if member_counts:
        df_member_rank = pd.DataFrame(
            sorted(member_counts.items(), key=lambda item: (-item[1], item[0])),
            columns=["member_name", "count"],
        )
    else:
        df_member_rank = pd.DataFrame(columns=["member_name", "count"])

    if band_stats:
        df_band_rank = pd.DataFrame(
            [
                (name, stats["count"], stats["song_count"])
                for name, stats in sorted(
                    band_stats.items(), key=lambda item: (-item[1]["count"], item[0])
                )
            ],
            columns=["band_name", "count", "song_count"],
        )
    else:
        df_band_rank = pd.DataFrame(columns=["band_name", "count", "song_count"])

    if write_csv:
        for path_out in [out_bands_path, out_band_rank_path, out_member_rank_path]:
            path_out.parent.mkdir(parents=True, exist_ok=True)
        df_bands.to_csv(out_bands_path, index=False, encoding="utf-8")
        df_member_rank.to_csv(out_member_rank_path, index=False, encoding="utf-8")
        df_band_rank.to_csv(out_band_rank_path, index=False, encoding="utf-8")
        print(f"Export completed: {out_dir}")

    return out_dir, df_bands, df_band_rank, df_member_rank




def create_export_archive(out_dir: Path) -> Path:
    if not out_dir.exists():
        raise FileNotFoundError(f"Output directory not found: {out_dir}")
    archive_base = out_dir.parent / out_dir.name
    archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=out_dir)
    return Path(archive_path)


async def _gradio_scrape_handler(email: str, password: str, exclude_nickname: str):
    if not email or not password or not exclude_nickname:
        return ("Email, password, and nickname are required.", None, None, None, None)

    try:
        out_dir, df_bands, df_band_rank, df_member_rank = await main(email, password, exclude_nickname)
        archive_path = create_export_archive(out_dir)
        return (
            f"Export completed: {out_dir}",
            df_bands,
            df_band_rank,
            df_member_rank,
            str(archive_path),
        )
    except Exception as exc:
        print(f"Error during scraping: {exc}")
        return (f"Error: {exc}", None, None, None, None)


def launch_gradio(share: bool = False, inbrowser: bool = False):
    if gr is None:
        raise RuntimeError("Gradio is not installed. Run 'pip install gradio' to enable the UI.")

    interface = gr.Interface(
        fn=_gradio_scrape_handler,
        inputs=[
            gr.Textbox(label="Email"),
            gr.Textbox(label="Password", type="password"),
            gr.Textbox(label="Nickname to exclude"),
        ],
        outputs=[
            gr.Textbox(label="Status"),
            gr.Dataframe(label="Band List"),
            gr.Dataframe(label="Band Rank"),
            gr.Dataframe(label="Member Rank"),
            gr.File(label="Download CSVs"),
        ],
        title="Bandwith Scraper",
        description="Enter your Bandwith credentials to run the scraper via a Gradio UI.",
        allow_flagging="never",
    )
    interface.queue(concurrency_count=1, max_size=1)
    return interface.launch(share=share, inbrowser=inbrowser, show_api=False)

if __name__ == "__main__":
    user_email, user_password, user_nickname = prompt_credentials()
    asyncio.run(main(user_email, user_password, user_nickname))
