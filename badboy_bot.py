from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from html import escape, unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://badboy.ua"
LVIV_EVENTS_URL = f"{BASE_URL}/catalog/events/lviv/"
CITY = "lviv"
CITY_NAME = "Львів"
UA_TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
NOTIFY_ON_FIRST_RUN = os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"


@dataclass
class Event:
    id: str
    city: str
    city_name: str
    title: str
    url: str
    date_text: str
    event_date: str | None
    price: str
    event_type: str


def main() -> None:
    state = load_state()
    telegram = TelegramClient(os.getenv("TELEGRAM_BOT_TOKEN"))

    if not telegram.enabled:
        print("TELEGRAM_BOT_TOKEN is not set; running in dry-run mode.")

    events = fetch_lviv_events()
    print(f"Fetched {len(events)} events for {CITY}.")

    sync_lviv_events(events, state, telegram, build_destinations())
    prune_old_completed_events(state)
    save_state(state)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()

    with STATE_PATH.open("r", encoding="utf-8") as file:
        raw_state = json.load(file)

    state = default_state()
    state["completed_events"] = raw_state.get("completed_events", {})

    raw_seen_events = raw_state.get("seen_events", {})
    if isinstance(raw_seen_events.get(CITY), dict):
        state["seen_events"] = {CITY: raw_seen_events[CITY]}
    elif isinstance(raw_seen_events, dict):
        state["seen_events"] = {CITY: raw_seen_events}

    state["bootstrapped"] = bool(raw_state.get("bootstrapped")) or CITY in raw_state.get(
        "bootstrapped_cities", []
    )
    return state


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "bootstrapped": False,
        "seen_events": {CITY: {}},
        "completed_events": {},
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fetch_lviv_events() -> list[Event]:
    events: list[Event] = []
    seen_urls: set[str] = set()
    next_url: str | None = LVIV_EVENTS_URL

    for _ in range(MAX_PAGES):
        if not next_url:
            break

        response = requests.get(next_url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for card in soup.select("article.ticket-card"):
            event = parse_event_card(card)
            if event and event.url not in seen_urls:
                events.append(event)
                seen_urls.add(event.url)

        next_link = soup.select_one("a.next.page-numbers")
        next_url = urljoin(next_url, next_link["href"]) if next_link and next_link.get("href") else None

    return events


def parse_event_card(card: BeautifulSoup) -> Event | None:
    title_tag = card.select_one(".ticket-card__title")
    date_tag = card.select_one(".ticket-card__datetime")
    price_tag = card.select_one(".ticket-card__price")
    type_tag = card.select_one(".ticket-card__type")
    link_tag = title_tag.find_parent("a") if title_tag else None

    if not title_tag or not date_tag or not link_tag or not link_tag.get("href"):
        return None

    url = normalize_url(urljoin(BASE_URL, link_tag["href"]))
    date_text = clean_text(date_tag.get_text(" ", strip=True))
    title = clean_text(title_tag.get_text(" ", strip=True))
    event_date = parse_event_date(date_text)

    return Event(
        id=event_id(url),
        city=CITY,
        city_name=CITY_NAME,
        title=title,
        url=url,
        date_text=date_text,
        event_date=event_date.isoformat() if event_date else None,
        price=clean_text(price_tag.get_text(" ", strip=True)) if price_tag else "",
        event_type=clean_text(type_tag.get_text(" ", strip=True)) if type_tag else "",
    )


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def event_id(url: str) -> str:
    digest = hashlib.sha1(f"{CITY}:{url}".encode("utf-8")).hexdigest()
    return digest[:16]


def parse_event_date(date_text: str) -> date | None:
    match = re.search(r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})", date_text)
    if not match:
        return None

    today = datetime.now(UA_TIMEZONE).date()
    parsed = date(today.year, int(match.group("month")), int(match.group("day")))

    # The site omits the year. If a listed event looks far in the past, it is
    # probably for early next year.
    if (today - parsed).days > 90:
        parsed = date(today.year + 1, parsed.month, parsed.day)
    return parsed


def sync_lviv_events(
    events: list[Event],
    state: dict[str, Any],
    telegram: "TelegramClient",
    destinations: set[str],
) -> None:
    seen_events = state.setdefault("seen_events", {}).setdefault(CITY, {})
    completed_events = state.setdefault("completed_events", {})
    is_bootstrapped = bool(state.get("bootstrapped"))

    current_ids = {event.id for event in events}
    for event_id_to_remove in list(seen_events):
        if event_id_to_remove not in current_ids:
            del seen_events[event_id_to_remove]

    for event in events:
        if completed_events.get(event.id) == event.event_date:
            continue

        is_new = event.id not in seen_events
        seen_events[event.id] = asdict(event)

        if is_new and (is_bootstrapped or NOTIFY_ON_FIRST_RUN):
            tickets_left = safe_fetch_tickets_left(event.url)
            send_to_destinations(
                destinations,
                telegram,
                format_new_event_message(event, tickets_left),
            )

    if not is_bootstrapped:
        state["bootstrapped"] = True

    send_today_reminders(state, telegram, destinations)


def send_today_reminders(
    state: dict[str, Any],
    telegram: "TelegramClient",
    destinations: set[str],
) -> None:
    today = datetime.now(UA_TIMEZONE).date().isoformat()
    seen_events = state.setdefault("seen_events", {}).setdefault(CITY, {})

    for event_id_value, event_data in list(seen_events.items()):
        if event_data.get("event_date") != today:
            continue

        tickets_left = fetch_tickets_left(event_data["url"])
        if tickets_left == 0:
            print(f"Skipping sold-out event today: {event_data.get('title', event_id_value)}")
            del seen_events[event_id_value]
            state.setdefault("completed_events", {})[event_id_value] = today
            continue

        if not destinations:
            continue

        event = Event(**event_data)
        sent = send_to_destinations(
            destinations,
            telegram,
            format_today_message(event, tickets_left),
        )
        if sent:
            del seen_events[event_id_value]
            state.setdefault("completed_events", {})[event_id_value] = today


def fetch_tickets_left(url: str) -> int | None:
    response = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_tickets_left(response.text)


def parse_tickets_left(html: str) -> int | None:
    soup = BeautifulSoup(html, "html.parser")
    quantity_info = soup.select_one(".ticket__quantity-info")
    if quantity_info:
        match = re.search(r"Залишилося\s+(\d+)", quantity_info.get_text(" ", strip=True))
        if match:
            return int(match.group(1))

    match = re.search(r'"quantity"\s*:\s*(\d+)', html)
    return int(match.group(1)) if match else None


def safe_fetch_tickets_left(url: str) -> int | None:
    try:
        return fetch_tickets_left(url)
    except Exception as error:
        print(f"Failed to fetch ticket count for {url}: {error}")
        return None


def build_destinations() -> set[str]:
    return chat_ids_from_env("TELEGRAM_TARGET_CHAT_ID_LVIV") | chat_ids_from_env(
        "TELEGRAM_TARGET_CHAT_ID"
    )


def chat_ids_from_env(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {chat_id.strip() for chat_id in raw.split(",") if chat_id.strip()}


def send_to_destinations(
    destinations: set[str],
    telegram: "TelegramClient",
    text: str,
) -> bool:
    sent_any = False
    for chat_id in sorted(destinations):
        try:
            telegram.send_message(chat_id, text)
        except Exception as error:
            print(f"Failed to send message to {chat_id}: {error}")
            continue
        sent_any = True
    return sent_any


def format_new_event_message(event: Event, tickets_left: int | None) -> str:
    details = [
        f"Нова подія BadBoy: {escape(event.city_name)}",
        f"<b>{escape(event.title)}</b>",
        f"Коли: {escape(event.date_text)}",
    ]
    if event.price:
        details.append(f"Ціна: {escape(event.price)}")
    details.append(format_ticket_link(event.url, tickets_left))
    return "\n".join(details)


def format_today_message(event: Event, tickets_left: int | None) -> str:
    tickets = f"{tickets_left} квитків" if tickets_left is not None else "кількість квитків невідома"
    return "\n".join(
        [
            f"Сьогодні подія BadBoy: {escape(event.city_name)}",
            f"<b>{escape(event.title)}</b>",
            f"Коли: {escape(event.date_text)}",
            f"Залишилось: {escape(tickets)}",
            format_ticket_link(event.url, tickets_left),
        ]
    )


def html_link(label: str, url: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'


def format_ticket_link(url: str, tickets_left: int | None) -> str:
    if tickets_left is None:
        return html_link("Квитки", url)
    return f'{html_link("Квитки", url)} - залишилось {tickets_left}'


def prune_old_completed_events(state: dict[str, Any]) -> None:
    today = datetime.now(UA_TIMEZONE).date()
    completed_events = state.setdefault("completed_events", {})
    for event_id_value, event_date_value in list(completed_events.items()):
        try:
            event_date_value_as_date = date.fromisoformat(event_date_value)
        except ValueError:
            del completed_events[event_id_value]
            continue
        if (today - event_date_value_as_date).days > 14:
            del completed_events[event_id_value]


class TelegramClient:
    def __init__(self, token: str | None) -> None:
        self.token = token
        self.enabled = bool(token)
        self.api_base = f"https://api.telegram.org/bot{token}" if token else ""

    def send_message(self, chat_id: str, text: str) -> None:
        if not self.enabled:
            print(f"[dry-run] Would send to {chat_id}: {text}")
            return

        response = requests.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": False,
                "parse_mode": "HTML",
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        payload = parse_telegram_response(response, "sendMessage")
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {payload}")


def parse_telegram_response(response: requests.Response, method: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(
            f"Telegram {method} returned HTTP {response.status_code}: {response.text}"
        ) from error

    if response.status_code >= 400:
        description = payload.get("description", response.text)
        raise RuntimeError(
            f"Telegram {method} returned HTTP {response.status_code}: {description}"
        )

    return payload


if __name__ == "__main__":
    main()
