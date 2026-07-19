from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any

from deal_radar.http import HttpError, post_form
from deal_radar.models import Listing, Valuation


SOURCE_LABELS = {"bazos": "Bazoš", "cyklobazar": "Cyklobazar"}
SOURCE_CALLBACK_CODES = {"bazos": "b", "cyklobazar": "c"}
CALLBACK_CODE_SOURCES = {code: source for source, code in SOURCE_CALLBACK_CODES.items()}


def format_czk(value: int | None) -> str:
    return f"{value:,} Kč".replace(",", " ") if value is not None else "цена не распознана"


class TelegramClient:
    def __init__(self, token: str, chat_id: str = "", timeout: int = 30) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _call(self, method: str, fields: dict[str, Any]) -> dict[str, Any]:
        return post_form(self._url(method), fields, timeout=self.timeout)["result"]

    def send_text(self, text: str, reply_to_message_id: int | None = None) -> int:
        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is not configured")
        fields: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }
        if reply_to_message_id:
            fields["reply_parameters"] = json.dumps({"message_id": reply_to_message_id})
        return int(self._call("sendMessage", fields)["message_id"])

    def send_listing(self, listing: Listing, retail_enabled: bool) -> int:
        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is not configured")
        title = html.escape(listing.title[:220])
        location = html.escape(listing.location or "локация не распознана")
        published = ""
        if listing.published_at:
            published = listing.published_at.astimezone().strftime("%d.%m %H:%M")
        marketplace = SOURCE_LABELS.get(listing.source, listing.source)
        ai_line = "🔎 Ищу цены нового велосипеда…" if retail_enabled else "🔎 Поиск новой цены выключен"
        text = (
            f"🚲 <b>Новое на {html.escape(marketplace)}</b>\n\n"
            f"<b>{title}</b>\n"
            f"💰 {format_czk(listing.price_czk)}\n"
            f"📍 {location}\n"
            + (f"🕒 {published}\n" if published else "")
            + f"🎯 {html.escape(listing.profile)}\n\n"
            f"{ai_line}\n"
            f"<a href=\"{html.escape(listing.url, quote=True)}\">Открыть объявление</a>"
        )
        callback_id = listing.external_id[:32]
        source_code = SOURCE_CALLBACK_CODES.get(listing.source, listing.source[:12])
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Интересно", "callback_data": f"fb:i:{source_code}:{callback_id}"},
                    {"text": "❌ Мимо", "callback_data": f"fb:n:{source_code}:{callback_id}"},
                ],
                [
                    {"text": "💸 Дорого", "callback_data": f"fb:e:{source_code}:{callback_id}"},
                    {"text": "⚠️ Подозрительно", "callback_data": f"fb:s:{source_code}:{callback_id}"},
                ],
            ]
        }
        common: dict[str, Any] = {
            "chat_id": self.chat_id,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(keyboard, ensure_ascii=False),
        }
        if listing.image_url:
            try:
                result = self._call("sendPhoto", {**common, "photo": listing.image_url, "caption": text})
                return int(result["message_id"])
            except HttpError:
                pass
        result = self._call(
            "sendMessage",
            {**common, "text": text, "disable_web_page_preview": "false"},
        )
        return int(result["message_id"])

    def send_valuation(
        self,
        listing: Listing,
        valuation: Valuation,
        reply_to_message_id: int,
        max_sources: int = 5,
    ) -> int:
        confidence_labels = {
            "high": "высокая",
            "medium": "средняя",
            "low": "низкая",
            "none": "нет надежного совпадения",
        }
        try:
            checked = datetime.fromisoformat(valuation.checked_at).astimezone().strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            checked = datetime.now().strftime("%d.%m.%Y")
        lines = ["💰 <b>Сравнение с новым велосипедом</b>"]
        if valuation.identified_product:
            lines.append(f"Модель: {html.escape(valuation.identified_product[:220])}")
        if valuation.median_price_czk is not None:
            lines.extend(
                [
                    f"Цена объявления: <b>{format_czk(listing.price_czk)}</b>",
                    f"Медианная цена нового: <b>{format_czk(valuation.median_price_czk)}</b>",
                ]
            )
            if valuation.discount_percent is not None:
                if valuation.discount_percent >= 0:
                    difference = f"дешевле на {valuation.discount_percent}%"
                else:
                    difference = f"дороже на {abs(valuation.discount_percent)}%"
                lines.append(f"Разница: <b>{difference}</b>")
            lines.extend(
                [
                    f"Найдено предложений: {valuation.source_count}",
                    f"Уверенность: {confidence_labels.get(valuation.confidence, valuation.confidence)}",
                    f"Проверено: {checked}" + (" · кэш" if valuation.status == "cached" else ""),
                ]
            )
        else:
            status_messages = {
                "ambiguous_model": "Не удалось точно определить модель велосипеда.",
                "model_discontinued": "Точная модель, вероятно, снята с продажи; активных предложений недостаточно.",
                "source_error": "Источники цен временно недоступны.",
                "codex_unavailable": "Неоднозначные совпадения требуют проверки, но Codex сейчас недоступен.",
                "timeout": "Поиск цены превысил допустимое время.",
            }
            lines.append(status_messages.get(valuation.status, "Не удалось найти минимум 3 точных предложения."))
            lines.append(f"Найдено точных совпадений: {valuation.source_count}")
            lines.append("Результат не используется для оценки выгоды.")
            lines.append(f"Проверено: {checked}")

        if valuation.comparables:
            lines.append("\nИсточники:")
            for comparable in valuation.comparables[:max_sources]:
                name = html.escape(comparable.seller[:80])
                url = html.escape(comparable.url, quote=True)
                lines.append(f"• <a href=\"{url}\">{name}</a> — {format_czk(comparable.price_czk)}")
        if valuation.notes and valuation.status in {"source_error", "codex_unavailable", "timeout"}:
            lines.append(f"\n⚠️ {html.escape(valuation.notes[:300])}")
        return self.send_text("\n".join(lines), reply_to_message_id=reply_to_message_id)

    def discover_chat_ids(self) -> list[dict[str, str]]:
        result = self._call("getUpdates", {"timeout": 0, "limit": 100})
        chats: dict[str, dict[str, str]] = {}
        for update in result:
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if "id" in chat:
                chat_id = str(chat["id"])
                chats[chat_id] = {
                    "id": chat_id,
                    "type": str(chat.get("type", "")),
                    "name": str(chat.get("title") or chat.get("username") or chat.get("first_name") or ""),
                }
        return list(chats.values())

    def poll_feedback(self, offset: int = 0) -> tuple[list[dict[str, str]], int]:
        updates = self._call("getUpdates", {"offset": offset, "timeout": 0, "limit": 100})
        feedback: list[dict[str, str]] = []
        next_offset = offset
        labels = {"i": "interesting", "n": "skip", "e": "expensive", "s": "suspicious"}
        for update in updates:
            next_offset = max(next_offset, int(update["update_id"]) + 1)
            query = update.get("callback_query")
            if not query:
                continue
            parts = str(query.get("data", "")).split(":")
            if len(parts) == 3:
                # Compatibility with Bazoš buttons sent by the first prototype.
                prefix, label_code, external_id = parts
                source = "bazos"
            elif len(parts) == 4:
                prefix, label_code, source_code, external_id = parts
                source = CALLBACK_CODE_SOURCES.get(source_code, source_code)
            else:
                continue
            if prefix != "fb" or label_code not in labels or not external_id:
                continue
            user = query.get("from", {})
            feedback.append(
                {
                    "source": source,
                    "external_id": external_id,
                    "label": labels[label_code],
                    "user": str(user.get("username") or user.get("id") or ""),
                }
            )
            try:
                self._call(
                    "answerCallbackQuery",
                    {"callback_query_id": query["id"], "text": "Сохранил оценку"},
                )
            except HttpError:
                pass
        return feedback, next_offset
