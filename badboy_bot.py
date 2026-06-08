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
CITY_URLS = {
    "kyiv": f"{BASE_URL}/catalog/events/kyiv/",
    "lviv": f"{BASE_URL}/catalog/events/lviv/",
    "dnipro": f"{BASE_URL}/catalog/events/dnipro/",
}
CITY_NAMES = {
    "kyiv": "Київ",
    "lviv": "Львів",
    "dnipro": "Дніпро",
}
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

    if telegram.enabled:
        process_telegram_commands(telegram, state)
    else:
        print("TELEGRAM_BOT_TOKEN is not set; running in dry-run mode.")

    cities_to_check = cities_from_env("FOLLOW_CITIES") | cities_with_subscribers(state)
    if not cities_to_check:
        cities_to_check = {"lviv"}
        print("No followed cities configured yet; checking lviv as the default baseline.")

    all_events: dict[str, list[Event]] = {}
    for city in sorted(cities_to_check):
        events = fetch_city_events(city)
        all_events[city] = events
        print(f"Fetched {len(events)} events for {city}.")

    destinations = build_destinations(state)
    for city, events in all_events.items():
        sync_city_events(city, events, state, telegram, destinations)

    prune_old_completed_events(state)
    save_state(state)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()
    with STATE_PATH.open("r", encoding="utf-8") as file:
        state = json.load(file)
    merged = default_state()
    merged.update(state)
    merged["subscribers"] = state.get("subscribers", {})
    merged["seen_events"] = state.get("seen_events", {})
    merged["completed_events"] = state.get("completed_events", {})
    merged["bootstrapped_cities"] = state.get("bootstrapped_cities", [])
    return merged


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "telegram_update_offset": None,
        "subscribers": {},
        "seen_events": {},
        "completed_events": {},
        "bootstrapped_cities": [],
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def cities_from_env(name: str) -> set[str]:
    raw = os.getenv(name, "")
    cities = {normalize_city(item) for item in raw.split(",") if item.strip()}
    return {city for city in cities if city in CITY_URLS}


def normalize_city(value: str) -> str:
    normalized = value.strip().lower()
    aliases = {
        "kiev": "kyiv",
        "київ": "kyiv",
        "киев": "kyiv",
        "львів": "lviv",
        "львов": "lviv",
        "дніпро": "dnipro",
        "днепр": "dnipro",
    }
    return aliases.get(normalized, normalized)


def cities_with_subscribers(state: dict[str, Any]) -> set[str]:
    cities: set[str] = set()
    for followed in state.get("subscribers", {}).values():
        cities.update(city for city in followed if city in CITY_URLS)
    return cities


def fetch_city_events(city: str) -> list[Event]:
    if city not in CITY_URLS:
        raise ValueError(f"Unsupported city: {city}")

    events: list[Event] = []
    seen_urls: set[str] = set()
    next_url: str | None = CITY_URLS[city]

    for _ in range(MAX_PAGES):
        if not next_url:
            break

        response = requests.get(next_url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for card in soup.select("article.ticket-card"):
            event = parse_event_card(card, city)
            if event and event.url not in seen_urls:
                events.append(event)
                seen_urls.add(event.url)

        next_link = soup.select_one("a.next.page-numbers")
        next_url = urljoin(next_url, next_link["href"]) if next_link and next_link.get("href") else None

    return events


def parse_event_card(card: BeautifulSoup, city: str) -> Event | None:
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
        id=event_id(city, url),
        city=city,
        city_name=CITY_NAMES[city],
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


def event_id(city: str, url: str) -> str:
    digest = hashlib.sha1(f"{city}:{url}".encode("utf-8")).hexdigest()
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


def sync_city_events(
    city: str,
    events: list[Event],
    state: dict[str, Any],
    telegram: "TelegramClient",
    destinations: dict[str, set[str]],
) -> None:
    seen_events = state.setdefault("seen_events", {}).setdefault(city, {})
    completed_events = state.setdefault("completed_events", {})
    bootstrapped_cities = set(state.setdefault("bootstrapped_cities", []))
    is_bootstrapped = city in bootstrapped_cities

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
            send_to_city_destinations(
                city,
                destinations,
                telegram,
                format_new_event_message(event, tickets_left),
            )

    if city not in bootstrapped_cities:
        bootstrapped_cities.add(city)
        state["bootstrapped_cities"] = sorted(bootstrapped_cities)

    send_today_reminders(city, state, telegram, destinations)


def send_today_reminders(
    city: str,
    state: dict[str, Any],
    telegram: "TelegramClient",
    destinations: dict[str, set[str]],
) -> None:
    today = datetime.now(UA_TIMEZONE).date().isoformat()
    seen_events = state.setdefault("seen_events", {}).setdefault(city, {})

    for event_id_value, event_data in list(seen_events.items()):
        if event_data.get("event_date") != today:
            continue

        if not destinations.get(city):
            continue

        tickets_left = fetch_tickets_left(event_data["url"])
        event = Event(**event_data)
        sent = send_to_city_destinations(
            city,
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
    soup = BeautifulSoup(response.text, "html.parser")

    quantity_info = soup.select_one(".ticket__quantity-info")
    if quantity_info:
        match = re.search(r"Залишилося\s+(\d+)", quantity_info.get_text(" ", strip=True))
        if match:
            return int(match.group(1))

    match = re.search(r'"quantity"\s*:\s*(\d+)', response.text)
    return int(match.group(1)) if match else None


def safe_fetch_tickets_left(url: str) -> int | None:
    try:
        return fetch_tickets_left(url)
    except Exception as error:
        print(f"Failed to fetch ticket count for {url}: {error}")
        return None


def build_destinations(state: dict[str, Any]) -> dict[str, set[str]]:
    destinations: dict[str, set[str]] = {city: set() for city in CITY_URLS}

    for chat_id, cities in state.get("subscribers", {}).items():
        for city in cities:
            if city in destinations:
                destinations[city].add(str(chat_id))

    for city in CITY_URLS:
        target_chat_id = os.getenv(f"TELEGRAM_TARGET_CHAT_ID_{city.upper()}")
        if target_chat_id:
            destinations[city].add(target_chat_id)

    generic_target_chat_id = os.getenv("TELEGRAM_TARGET_CHAT_ID")
    if generic_target_chat_id:
        generic_target_cities = cities_from_env("FOLLOW_CITIES") or {"lviv"}
        for city in generic_target_cities:
            destinations[city].add(generic_target_chat_id)

    return destinations


def send_to_city_destinations(
    city: str,
    destinations: dict[str, set[str]],
    telegram: "TelegramClient",
    text: str,
) -> bool:
    sent_any = False
    for chat_id in sorted(destinations.get(city, [])):
        try:
            telegram.send_message(chat_id, text)
        except Exception as error:
            print(f"Failed to send {city} message to {chat_id}: {error}")
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


def process_telegram_commands(telegram: "TelegramClient", state: dict[str, Any]) -> None:
    offset = state.get("telegram_update_offset")
    updates = telegram.get_updates(offset)

    for update in updates:
        state["telegram_update_offset"] = update["update_id"] + 1
        message = update.get("message") or update.get("channel_post")
        if not message:
            continue

        text = message.get("text", "").strip()
        chat_id = str(message.get("chat", {}).get("id", ""))
        if not text or not chat_id:
            continue

        response = handle_command(text, chat_id, state)
        if response:
            telegram.send_message(chat_id, response)


def handle_command(text: str, chat_id: str, state: dict[str, Any]) -> str | None:
    command, *args = text.split()
    command = command.split("@", 1)[0].lower()

    subscribers = state.setdefault("subscribers", {})
    followed = set(subscribers.get(chat_id, []))

    if command in {"/start", "/help"}:
        return (
            "Команди:\n"
            "/follow lviv|kyiv|dnipro\n"
            "/unfollow lviv|kyiv|dnipro\n"
            "/followall\n"
            "/unfollowall\n"
            "/cities"
        )

    if command == "/cities":
        if not followed:
            return "Ти поки не слідкуєш за жодним містом."
        city_names = ", ".join(CITY_NAMES[city] for city in sorted(followed))
        return f"Слідкую за: {city_names}"

    if command == "/followall":
        subscribers[chat_id] = sorted(CITY_URLS)
        return "Готово. Слідкую за Київ, Львів, Дніпро."

    if command == "/unfollowall":
        subscribers.pop(chat_id, None)
        return "Готово. Відписав цей чат від усіх міст."

    if command.startswith("/follow_"):
        city = normalize_city(command.removeprefix("/follow_"))
    elif command == "/follow" and args:
        city = normalize_city(args[0])
    else:
        city = ""

    if city in CITY_URLS:
        followed.add(city)
        subscribers[chat_id] = sorted(followed)
        return f"Готово. Слідкую за {CITY_NAMES[city]}."

    if command.startswith("/unfollow_"):
        city = normalize_city(command.removeprefix("/unfollow_"))
    elif command == "/unfollow" and args:
        city = normalize_city(args[0])
    else:
        city = ""

    if city in CITY_URLS:
        followed.discard(city)
        if followed:
            subscribers[chat_id] = sorted(followed)
        else:
            subscribers.pop(chat_id, None)
        return f"Готово. Більше не слідкую за {CITY_NAMES[city]}."

    return None


class TelegramClient:
    def __init__(self, token: str | None) -> None:
        self.token = token
        self.enabled = bool(token)
        self.api_base = f"https://api.telegram.org/bot{token}" if token else ""

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        response = requests.get(
            f"{self.api_base}/getUpdates",
            params=params,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        payload = parse_telegram_response(response, "getUpdates")
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
        return payload.get("result", [])

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
