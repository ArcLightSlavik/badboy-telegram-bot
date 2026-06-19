from __future__ import annotations

from datetime import datetime

import badboy_bot


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_message(self, chat_id: str, text: str) -> None:
        self.messages.append((chat_id, text))


def make_event(event_date: str) -> badboy_bot.Event:
    url = "https://badboy.ua/product/test-event/"
    return badboy_bot.Event(
        id=badboy_bot.event_id(url),
        city=badboy_bot.CITY,
        city_name=badboy_bot.CITY_NAME,
        title="Test Event",
        url=url,
        date_text="20.06, Сб, 19:00",
        event_date=event_date,
        price="1000 грн",
        event_type="Дегустація",
    )


def test_build_destinations_accepts_multiple_lviv_channels(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TARGET_CHAT_ID_LVIV", "@one, @two")
    monkeypatch.delenv("TELEGRAM_TARGET_CHAT_ID", raising=False)

    assert badboy_bot.build_destinations() == {"@one", "@two"}


def test_parse_tickets_left_reads_zero_from_product_page_html():
    html = """
    <div class="ticket__quantity-info">
        <p><b>Квитки:</b>&nbsp;&nbsp; Залишилося 0 штук</p>
    </div>
    """

    assert badboy_bot.parse_tickets_left(html) == 0


def test_parse_tickets_left_falls_back_to_ga_quantity():
    html = """
    <script>
    var gaEvents = [{"event":"view_item","ecommerce":{"items":[{"quantity":0}]}}];
    </script>
    """

    assert badboy_bot.parse_tickets_left(html) == 0


def test_load_state_migrates_existing_multi_city_state(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        """
        {
          "bootstrapped_cities": ["other_city", "lviv"],
          "completed_events": {"old": "2026-06-18"},
          "seen_events": {
            "other_city": {"drop": {"title": "Other city"}},
            "lviv": {"keep": {"title": "Lviv"}}
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(badboy_bot, "STATE_PATH", state_path)

    state = badboy_bot.load_state()

    assert state["bootstrapped"] is True
    assert state["seen_events"] == {"lviv": {"keep": {"title": "Lviv"}}}
    assert state["completed_events"] == {"old": "2026-06-18"}


def test_sold_out_today_event_is_not_sent_and_is_completed(monkeypatch):
    today = datetime.now(badboy_bot.UA_TIMEZONE).date().isoformat()
    event = make_event(today)
    state = {
        "schema_version": 2,
        "bootstrapped": True,
        "seen_events": {badboy_bot.CITY: {event.id: badboy_bot.asdict(event)}},
        "completed_events": {},
    }
    telegram = FakeTelegram()

    monkeypatch.setattr(badboy_bot, "fetch_tickets_left", lambda _: 0)

    badboy_bot.send_today_reminders(state, telegram, {"@lviv"})

    assert telegram.messages == []
    assert state["seen_events"][badboy_bot.CITY] == {}
    assert state["completed_events"] == {event.id: today}


def test_today_event_with_tickets_is_sent_and_completed(monkeypatch):
    today = datetime.now(badboy_bot.UA_TIMEZONE).date().isoformat()
    event = make_event(today)
    state = {
        "schema_version": 2,
        "bootstrapped": True,
        "seen_events": {badboy_bot.CITY: {event.id: badboy_bot.asdict(event)}},
        "completed_events": {},
    }
    telegram = FakeTelegram()

    monkeypatch.setattr(badboy_bot, "fetch_tickets_left", lambda _: 3)

    badboy_bot.send_today_reminders(state, telegram, {"@lviv"})

    assert len(telegram.messages) == 1
    assert "Залишилось: 3 квитків" in telegram.messages[0][1]
    assert state["seen_events"][badboy_bot.CITY] == {}
    assert state["completed_events"] == {event.id: today}
