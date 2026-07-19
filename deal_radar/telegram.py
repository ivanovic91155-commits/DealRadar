from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from typing import Any

from deal_radar.http import HttpError, post_form
from deal_radar.models import Listing, Valuation


SOURCE_LABELS = {"bazos": "Bazoš", "cyklobazar": "Cyklobazar"}
SOURCE_CALLBACK_CODES = {"bazos": "b", "cyklobazar": "c"}
CALLBACK_CODE_SOURCES = {code: source for source, code in SOURCE_CALLBACK_CODES.items()}
LOGGER = logging.getLogger(__name__)


def format_czk(value: int | None) -> str:
    return f"{value:,} Kč".replace(",", " ") if value is not None else "цена не распознана"


def format_seller_price(listing: Listing) -> str:
    status_labels = {
        "negotiable": "договорная",
        "free": "бесплатно",
        "in_description": "указана в описании",
        "missing": "не указана",
        "parse_error": "не удалось распознать",
    }
    if listing.price_status == "numeric":
        amount = listing.price_amount if listing.price_amount is not None else listing.price_czk
        if amount is None:
            return "не удалось распознать"
        if listing.currency.upper() == "CZK":
            return format_czk(amount)
        return f"{amount:,} {listing.currency.upper()}".replace(",", " ")
    if listing.price_status in status_labels:
        return status_labels[listing.price_status]
    return format_czk(listing.price_czk)


def price_origin_label(value: str) -> str:
    return {
        "list_page": "list page",
        "detail_page": "detail page",
        "not_found": "not found",
    }.get(value, "not found")


def _listing_keyboard(listing: Listing, *, classifications: bool = True) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    if classifications:
        callback_id = listing.external_id[:32]
        source_code = SOURCE_CALLBACK_CODES.get(listing.source, listing.source[:12])
        rows.extend(
            [
                [
                    {"text": "✅ Интересно", "callback_data": f"fb:i:{source_code}:{callback_id}"},
                    {"text": "❌ Мимо", "callback_data": f"fb:n:{source_code}:{callback_id}"},
                ],
                [
                    {"text": "💸 Дорого", "callback_data": f"fb:e:{source_code}:{callback_id}"},
                    {"text": "⚠️ Подозрительно", "callback_data": f"fb:s:{source_code}:{callback_id}"},
                ],
            ]
        )
    rows.append([{"text": "🔗 Открыть объявление", "url": listing.url}])
    return {"inline_keyboard": rows}


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

    def send_listing(
        self,
        listing: Listing,
        retail_enabled: bool,
        diagnostic_header: str = "",
    ) -> int:
        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is not configured")
        title = html.escape(listing.title[:220])
        location = html.escape(listing.location or "локация не распознана")
        published = ""
        if listing.published_at:
            published = listing.published_at.astimezone().strftime("%d.%m %H:%M")
        marketplace = SOURCE_LABELS.get(listing.source, listing.source)
        ai_line = "🔎 Ищу цены нового велосипеда…" if retail_enabled else "🔎 Поиск новой цены выключен"
        price_line = (
            f"💰 Цена продавца: {format_seller_price(listing)}\n"
            f"🧾 Источник цены: {price_origin_label(listing.price_origin)}\n"
            if listing.source == "cyklobazar"
            else f"💰 {format_czk(listing.price_czk)}\n"
        )
        prefix = f"🧪 <b>{html.escape(diagnostic_header)}</b>\n\n" if diagnostic_header else ""
        text = (
            f"{prefix}"
            f"🚲 <b>Новое на {html.escape(marketplace)}</b>\n\n"
            f"<b>{title}</b>\n"
            f"{price_line}"
            f"📍 {location}\n"
            + (f"🕒 {published}\n" if published else "")
            + f"🎯 {html.escape(listing.profile)}\n\n"
            f"{ai_line}\n"
            f"<a href=\"{html.escape(listing.url, quote=True)}\">Открыть объявление</a>"
        )
        keyboard = _listing_keyboard(listing)
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

    def get_feedback_updates(self, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        updates = self._call("getUpdates", {"offset": offset, "timeout": 0, "limit": 100})
        feedback: list[dict[str, Any]] = []
        next_offset = offset
        labels = {"i": "interesting", "n": "skip", "e": "too_expensive", "s": "suspicious"}
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
            message = query.get("message") or {}
            chat = message.get("chat") or {}
            feedback.append(
                {
                    "source": source,
                    "external_id": external_id,
                    "label": labels[label_code],
                    "user": str(user.get("username") or user.get("id") or ""),
                    "callback_query_id": str(query.get("id") or ""),
                    "telegram_message_id": int(message["message_id"]) if "message_id" in message else None,
                    "telegram_chat_id": str(chat.get("id") or ""),
                    "message": message,
                }
            )
        return feedback, next_offset

    def _answer_feedback(self, event: dict[str, Any], text: str) -> None:
        callback_query_id = str(event.get("callback_query_id") or "")
        if not callback_query_id:
            return
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
        except HttpError as exc:
            LOGGER.warning("Telegram callback answer failed: %s", type(exc).__name__)

    @staticmethod
    def _existing_url_keyboard(message: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for row in (message.get("reply_markup") or {}).get("inline_keyboard", []):
            urls = [button for button in row if button.get("url")]
            if urls:
                rows.append(urls)
        return {"inline_keyboard": rows}

    def _mark_message(
        self,
        event: dict[str, Any],
        listing: Listing | None,
        marker: str,
    ) -> None:
        message = event.get("message") or {}
        chat_id = event.get("telegram_chat_id")
        message_id = event.get("telegram_message_id")
        if not chat_id or message_id is None:
            return
        is_photo = bool(message.get("photo"))
        current = str(message.get("caption") if is_photo else message.get("text") or "")
        if marker not in current:
            current = f"{current}\n\n{marker}".strip()
        keyboard = _listing_keyboard(listing, classifications=False) if listing else self._existing_url_keyboard(message)
        method = "editMessageCaption" if is_photo else "editMessageText"
        field = "caption" if is_photo else "text"
        self._call(
            method,
            {
                "chat_id": chat_id,
                "message_id": message_id,
                field: current,
                "reply_markup": json.dumps(keyboard, ensure_ascii=False),
            },
        )

    def apply_feedback_action(
        self,
        event: dict[str, Any],
        listing: Listing | None,
        *,
        repeated: bool = False,
    ) -> None:
        label = str(event.get("label") or "")
        confirmations = {
            "interesting": "Сохранено: интересно",
            "skip": "Убрано",
            "too_expensive": "Убрано: дорого",
            "suspicious": "Сохранено: подозрительно",
        }
        self._answer_feedback(event, confirmations.get(label, "Сохранено"))
        if repeated:
            return
        if label in {"skip", "too_expensive"}:
            try:
                self._call(
                    "deleteMessage",
                    {
                        "chat_id": event.get("telegram_chat_id"),
                        "message_id": event.get("telegram_message_id"),
                    },
                )
                return
            except HttpError as exc:
                LOGGER.warning("Telegram message delete failed; applying fallback: %s", type(exc).__name__)
                marker = "❌ Мимо" if label == "skip" else "💸 Мимо: дорого"
                try:
                    self._mark_message(event, listing, marker)
                except HttpError as edit_exc:
                    LOGGER.warning("Telegram delete fallback edit failed: %s", type(edit_exc).__name__)
                return
        marker = "⭐ Отмечено: интересно" if label == "interesting" else "⚠️ Отмечено: подозрительно"
        try:
            self._mark_message(event, listing, marker)
        except HttpError as exc:
            LOGGER.warning("Telegram feedback message edit failed: %s", type(exc).__name__)

    def poll_feedback(self, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        feedback, next_offset = self.get_feedback_updates(offset)
        for event in feedback:
            self._answer_feedback(event, "Сохранил оценку")
        return feedback, next_offset
