import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import ccxt.async_support as ccxt
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("btc-alert-bot")


@dataclass(frozen=True)
class AppConfig:
    telegram_bot_token: str
    telegram_chat_ids: tuple[int, ...]
    exchanges: tuple[str, ...]
    symbol: str

    @classmethod
    def from_env(cls) -> "AppConfig":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_chat_ids = os.getenv("TELEGRAM_CHAT_IDS", os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        if not token or not raw_chat_ids:
            raise ValueError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID(S) в файле .env")
        try:
            parsed_chat_ids = tuple(dict.fromkeys(
                int(item.strip()) for item in raw_chat_ids.split(",") if item.strip()
            ))
        except ValueError as exc:
            raise ValueError("Telegram ID должны быть целыми числами через запятую") from exc
        if not parsed_chat_ids:
            raise ValueError("Укажите хотя бы один Telegram ID")

        exchanges = tuple(
            item.strip().lower()
            for item in os.getenv("EXCHANGES", "binance,bybit,okx").split(",")
            if item.strip()
        )
        if not exchanges:
            raise ValueError("В EXCHANGES должна быть хотя бы одна биржа")
        return cls(token, parsed_chat_ids, exchanges, os.getenv("SYMBOL", "BTC/USDT"))


@dataclass
class UserSettings:
    lower_threshold: str = "90000"
    upper_threshold: str = "110000"
    interval_minutes: int = 3
    notifications_enabled: bool = True
    enabled_exchanges: list[str] = field(default_factory=list)


class UserSettingsView:
    def __init__(self, manager: "SettingsManager", chat_id: int):
        self.manager = manager
        self.chat_id = chat_id

    @property
    def data(self) -> UserSettings:
        return self.manager.users[str(self.chat_id)]

    def save(self) -> None:
        self.manager.save()

    @property
    def lower(self) -> Decimal:
        return Decimal(self.data.lower_threshold)

    @property
    def upper(self) -> Decimal:
        return Decimal(self.data.upper_threshold)


class SettingsManager:
    def __init__(self, path: Path, exchange_ids: tuple[str, ...], chat_ids: tuple[int, ...]):
        self.path = path
        self.exchange_ids = exchange_ids
        self.chat_ids = chat_ids
        self.users = self._load()
        for chat_id in chat_ids:
            key = str(chat_id)
            if key not in self.users:
                self.users[key] = self._defaults()
            valid = [item for item in self.users[key].enabled_exchanges if item in self.exchange_ids]
            self.users[key].enabled_exchanges = valid or list(self.exchange_ids)
        self.save()

    def _defaults(self) -> UserSettings:
        seconds = int(os.getenv("INTERVAL_SECONDS", "180"))
        return UserSettings(
            lower_threshold=os.getenv("LOWER_THRESHOLD", "90000"),
            upper_threshold=os.getenv("UPPER_THRESHOLD", "110000"),
            interval_minutes=max(1, min(5, seconds // 60)),
            enabled_exchanges=list(self.exchange_ids),
        )

    def _parse(self, raw: dict[str, Any]) -> UserSettings:
        defaults = self._defaults()
        return UserSettings(
            lower_threshold=str(raw.get("lower_threshold", defaults.lower_threshold)),
            upper_threshold=str(raw.get("upper_threshold", defaults.upper_threshold)),
            interval_minutes=max(1, min(5, int(raw.get("interval_minutes", defaults.interval_minutes)))),
            notifications_enabled=bool(raw.get("notifications_enabled", True)),
            enabled_exchanges=list(raw.get("enabled_exchanges", self.exchange_ids)),
        )

    def _load(self) -> dict[str, UserSettings]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if "users" in raw:
                return {str(chat_id): self._parse(data) for chat_id, data in raw["users"].items()}
            # Миграция старого формата: прежние настройки остаются у первого пользователя.
            return {str(self.chat_ids[0]): self._parse(raw)}
        except (OSError, ValueError, TypeError):
            logger.exception("Не удалось прочитать settings.json")
            return {}

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        payload = {"users": {chat_id: asdict(settings) for chat_id, settings in self.users.items()}}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def for_chat(self, chat_id: int) -> UserSettingsView:
        return UserSettingsView(self, chat_id)


class PriceInput(StatesGroup):
    lower = State()
    upper = State()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📈 Актуальная цена"), KeyboardButton(text="📊 Статус")],
            [KeyboardButton(text="💰 Рамки цены"), KeyboardButton(text="🏦 Биржи")],
            [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="⏱ Интервал")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def thresholds_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬇️ Нижняя граница", callback_data="threshold:lower")],
        [InlineKeyboardButton(text="⬆️ Верхняя граница", callback_data="threshold:upper")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="threshold:cancel")],
    ])


def threshold_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отменить редактирование", callback_data="threshold:cancel")
    ]])


def interval_keyboard(current: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"{'✅ ' if minute == current else ''}{minute} мин",
            callback_data=f"interval:{minute}",
        ) for minute in range(1, 6)
    ]])


def notifications_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    action = "off" if enabled else "on"
    text = "🔕 Выключить" if enabled else "🔔 Включить"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=f"notifications:{action}")
    ]])


def exchanges_keyboard(config: AppConfig, store: UserSettingsView) -> InlineKeyboardMarkup:
    enabled = set(store.data.enabled_exchanges)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅' if exchange_id in enabled else '❌'} {exchange_id.upper()}",
            callback_data=f"exchange:{exchange_id}",
        )]
        for exchange_id in config.exchanges
    ])


def create_exchange(exchange_id: str) -> Any:
    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"Биржа '{exchange_id}' не поддерживается CCXT")
    return exchange_class({"enableRateLimit": True, "timeout": 20_000})


async def fetch_price(exchange: Any, symbol: str) -> Decimal:
    ticker = await exchange.fetch_ticker(symbol)
    if ticker.get("last") is None:
        raise RuntimeError(f"{exchange.id} не вернула цену {symbol}")
    return Decimal(str(ticker["last"]))


def price_zone(price: Decimal, store: UserSettingsView) -> str:
    if price > store.upper:
        return "above"
    if price < store.lower:
        return "below"
    return "normal"


def format_price(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ")


async def current_prices_text(
    config: AppConfig,
    store: UserSettingsView,
    exchanges: dict[str, Any],
) -> str:
    enabled_ids = [item for item in config.exchanges if item in store.data.enabled_exchanges]
    results = await asyncio.gather(
        *(fetch_price(exchanges[exchange_id], config.symbol) for exchange_id in enabled_ids),
        return_exceptions=True,
    )
    prices: list[tuple[str, Decimal]] = []
    failed: list[str] = []
    for exchange_id, result in zip(enabled_ids, results):
        if isinstance(result, BaseException):
            logger.error("Ошибка запроса цены %s: %s", exchange_id, result)
            failed.append(exchange_id.upper())
        else:
            prices.append((exchange_id, result))

    if not prices:
        return "⚠️ Не удалось получить актуальную цену. Попробуйте ещё раз немного позже."

    sorted_values = sorted(price for _, price in prices)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        common_price = sorted_values[middle]
    else:
        common_price = (sorted_values[middle - 1] + sorted_values[middle]) / 2

    lines = [
        "₿ BTC / USDT",
        "",
        "ОБЩИЙ КУРС",
        f"💵 {format_price(common_price)} USDT",
        "",
        "Цены на биржах:",
    ]
    lines.extend(f"• {exchange_id.upper()}: {format_price(price)} USDT" for exchange_id, price in prices)
    if failed:
        lines.append(f"\nНет ответа: {', '.join(failed)}")
    lines.extend(["", f"Обновлено: {datetime.now().astimezone():%H:%M:%S}"])
    return "\n".join(lines)


def alert_text(exchange_id: str, symbol: str, price: Decimal, zone: str, store: UserSettingsView) -> str:
    if zone == "above":
        return (
            f"🚀 {symbol} выше верхней границы на {exchange_id.upper()}\n"
            f"Цена: {format_price(price)} USDT\nГраница: {format_price(store.upper)} USDT"
        )
    if zone == "below":
        return (
            f"⚠️ {symbol} ниже нижней границы на {exchange_id.upper()}\n"
            f"Цена: {format_price(price)} USDT\nГраница: {format_price(store.lower)} USDT"
        )
    return (
        f"✅ {symbol} вернулся в диапазон на {exchange_id.upper()}\n"
        f"Цена: {format_price(price)} USDT\n"
        f"Диапазон: {format_price(store.lower)}–{format_price(store.upper)} USDT"
    )


def status_text(config: AppConfig, store: UserSettingsView) -> str:
    enabled = "включены 🔔" if store.data.notifications_enabled else "выключены 🔕"
    return (
        "⚙️ Текущие настройки\n\n"
        f"Пара: {config.symbol}\n"
        f"Нижняя граница: {format_price(store.lower)} USDT\n"
        f"Верхняя граница: {format_price(store.upper)} USDT\n"
        f"Интервал: {store.data.interval_minutes} мин.\n"
        f"Уведомления: {enabled}\n"
        f"Активные биржи: {', '.join(item.upper() for item in store.data.enabled_exchanges)}"
    )


def is_allowed(event: Message | CallbackQuery, config: AppConfig) -> bool:
    message = event.message if isinstance(event, CallbackQuery) else event
    return bool(message and message.chat.id in config.telegram_chat_ids)


def build_router(
    config: AppConfig,
    manager: SettingsManager,
    exchanges: dict[str, Any],
    reset_monitor: Callable[[int], None],
) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        if not is_allowed(message, config):
            await message.answer(
                "⛔ Доступ пока не разрешён.\n\n"
                f"Ваш Telegram ID: {message.chat.id}\n"
                "Передайте этот ID владельцу бота."
            )
            return
        store = manager.for_chat(message.chat.id)
        await state.clear()
        await message.answer("🤖 Мониторинг BTC/USDT готов. Управление — кнопками ниже.", reply_markup=main_keyboard())
        await message.answer(status_text(config, store))

    @router.message(F.text == "📊 Статус")
    async def status(message: Message) -> None:
        if is_allowed(message, config):
            store = manager.for_chat(message.chat.id)
            await message.answer(status_text(config, store), reply_markup=main_keyboard())

    @router.message(F.text == "📈 Актуальная цена")
    async def current_price(message: Message) -> None:
        if not is_allowed(message, config):
            return
        store = manager.for_chat(message.chat.id)
        waiting = await message.answer("⏳ Получаю свежие цены...")
        await waiting.edit_text(await current_prices_text(config, store, exchanges))

    @router.message(F.text == "🏦 Биржи")
    async def exchange_settings(message: Message) -> None:
        if is_allowed(message, config):
            store = manager.for_chat(message.chat.id)
            await message.answer(
                "Выберите биржи для мониторинга и общего курса:",
                reply_markup=exchanges_keyboard(config, store),
            )

    @router.callback_query(F.data.startswith("exchange:"))
    async def exchange_switch(query: CallbackQuery) -> None:
        if not is_allowed(query, config):
            return
        store = manager.for_chat(query.message.chat.id)
        exchange_id = query.data.split(":", 1)[1]
        enabled = store.data.enabled_exchanges
        if exchange_id in enabled:
            if len(enabled) == 1:
                await query.answer("Нужно оставить хотя бы одну биржу", show_alert=True)
                return
            enabled.remove(exchange_id)
        else:
            enabled.append(exchange_id)
            enabled.sort(key=config.exchanges.index)
        store.save()
        reset_monitor(query.message.chat.id)
        await query.message.edit_text(
            "Выберите биржи для мониторинга и общего курса:",
            reply_markup=exchanges_keyboard(config, store),
        )
        await query.answer("Настройка сохранена")

    @router.message(F.text == "💰 Рамки цены")
    async def thresholds(message: Message, state: FSMContext) -> None:
        if not is_allowed(message, config):
            return
        store = manager.for_chat(message.chat.id)
        await state.clear()
        waiting = await message.answer("⏳ Получаю актуальную цену...")
        price_report = await current_prices_text(config, store, exchanges)
        await waiting.edit_text(
            price_report + "\n\nКакую границу изменить?",
            reply_markup=thresholds_keyboard(),
        )

    @router.callback_query(F.data.in_({"threshold:lower", "threshold:upper"}))
    async def threshold_choice(query: CallbackQuery, state: FSMContext) -> None:
        if not is_allowed(query, config):
            return
        kind = query.data.split(":", 1)[1]
        await state.set_state(PriceInput.lower if kind == "lower" else PriceInput.upper)
        label = "нижнюю" if kind == "lower" else "верхнюю"
        await query.message.answer(
            f"Введите {label} границу в USDT одним числом, например: 95000",
            reply_markup=threshold_cancel_keyboard(),
        )
        await query.answer()

    @router.callback_query(F.data == "threshold:cancel")
    async def threshold_cancel(query: CallbackQuery, state: FSMContext) -> None:
        if not is_allowed(query, config):
            return
        await state.clear()
        await query.message.edit_text("❌ Редактирование рамок отменено. Настройки не изменены.")
        await query.answer("Отменено")

    async def save_threshold(message: Message, state: FSMContext, kind: str) -> None:
        if not is_allowed(message, config):
            return
        store = manager.for_chat(message.chat.id)
        try:
            value = Decimal((message.text or "").replace(" ", "").replace(",", "."))
        except InvalidOperation:
            await message.answer("Не получилось распознать число. Введите, например: 95000")
            return
        if value <= 0:
            await message.answer("Цена должна быть больше нуля.")
            return
        if kind == "lower" and value >= store.upper:
            await message.answer("Нижняя граница должна быть меньше верхней.")
            return
        if kind == "upper" and value <= store.lower:
            await message.answer("Верхняя граница должна быть больше нижней.")
            return

        if kind == "lower":
            store.data.lower_threshold = str(value)
        else:
            store.data.upper_threshold = str(value)
        store.save()
        reset_monitor(message.chat.id)
        await state.clear()
        await message.answer("✅ Граница сохранена.\n\n" + status_text(config, store), reply_markup=main_keyboard())

    @router.message(PriceInput.lower)
    async def lower_input(message: Message, state: FSMContext) -> None:
        await save_threshold(message, state, "lower")

    @router.message(PriceInput.upper)
    async def upper_input(message: Message, state: FSMContext) -> None:
        await save_threshold(message, state, "upper")

    @router.message(F.text == "🔔 Уведомления")
    async def notifications(message: Message) -> None:
        if is_allowed(message, config):
            store = manager.for_chat(message.chat.id)
            enabled = store.data.notifications_enabled
            text = "Уведомления сейчас включены." if enabled else "Уведомления сейчас выключены."
            await message.answer(text, reply_markup=notifications_keyboard(enabled))

    @router.callback_query(F.data.startswith("notifications:"))
    async def notifications_switch(query: CallbackQuery) -> None:
        if not is_allowed(query, config):
            return
        store = manager.for_chat(query.message.chat.id)
        store.data.notifications_enabled = query.data.endswith(":on")
        store.save()
        reset_monitor(query.message.chat.id)
        text = "🔔 Уведомления включены" if store.data.notifications_enabled else "🔕 Уведомления выключены"
        await query.message.edit_text(text, reply_markup=notifications_keyboard(store.data.notifications_enabled))
        await query.answer()

    @router.message(F.text == "⏱ Интервал")
    async def interval(message: Message) -> None:
        if is_allowed(message, config):
            store = manager.for_chat(message.chat.id)
            await message.answer("Выберите интервал проверки:", reply_markup=interval_keyboard(store.data.interval_minutes))

    @router.callback_query(F.data.startswith("interval:"))
    async def interval_choice(query: CallbackQuery) -> None:
        if not is_allowed(query, config):
            return
        store = manager.for_chat(query.message.chat.id)
        minute = int(query.data.split(":", 1)[1])
        store.data.interval_minutes = minute
        store.save()
        reset_monitor(query.message.chat.id)
        await query.message.edit_text(f"✅ Интервал установлен: {minute} мин.", reply_markup=interval_keyboard(minute))
        await query.answer()

    return router


async def monitor_user(
    bot: Bot,
    config: AppConfig,
    manager: SettingsManager,
    exchanges: dict[str, Any],
    chat_id: int,
    last_zones: dict[str, str],
    wake_event: asyncio.Event,
) -> None:
    while True:
        store = manager.for_chat(chat_id)
        try:
            enabled_items = [
                (exchange_id, exchange)
                for exchange_id, exchange in exchanges.items()
                if exchange_id in store.data.enabled_exchanges
            ]
            for exchange_id in set(last_zones) - set(store.data.enabled_exchanges):
                last_zones.pop(exchange_id, None)
            results = await asyncio.gather(
                *(fetch_price(exchange, config.symbol) for _, exchange in enabled_items),
                return_exceptions=True,
            )
            for (exchange_id, _), result in zip(enabled_items, results):
                if isinstance(result, BaseException):
                    logger.error("Ошибка %s: %s", exchange_id, result)
                    continue
                price = result
                zone = price_zone(price, store)
                previous = last_zones.get(exchange_id)
                logger.info("%s %s = %s, зона: %s", exchange_id, config.symbol, price, zone)

                if store.data.notifications_enabled:
                    crossed = previous is not None and zone != previous
                    first_outside = previous is None and zone != "normal"
                    if crossed or first_outside:
                        await bot.send_message(
                            chat_id,
                            alert_text(exchange_id, config.symbol, price, zone, store),
                        )
                last_zones[exchange_id] = zone
        except Exception:
            logger.exception("Ошибка мониторинга пользователя %s", chat_id)
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=store.data.interval_minutes * 60)
            wake_event.clear()
        except TimeoutError:
            pass


async def main() -> None:
    config = AppConfig.from_env()
    manager = SettingsManager(
        BASE_DIR / "settings.json",
        config.exchanges,
        config.telegram_chat_ids,
    )
    exchanges = {exchange_id: create_exchange(exchange_id) for exchange_id in config.exchanges}
    last_zones: dict[int, dict[str, str]] = {chat_id: {} for chat_id in config.telegram_chat_ids}
    wake_events: dict[int, asyncio.Event] = {chat_id: asyncio.Event() for chat_id in config.telegram_chat_ids}

    def reset_monitor(chat_id: int) -> None:
        last_zones[chat_id].clear()
        wake_events[chat_id].set()

    bot = Bot(token=config.telegram_bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(build_router(config, manager, exchanges, reset_monitor))
    monitor_tasks = [
        asyncio.create_task(
            monitor_user(bot, config, manager, exchanges, chat_id, last_zones[chat_id], wake_events[chat_id])
        )
        for chat_id in config.telegram_chat_ids
    ]

    try:
        for chat_id in config.telegram_chat_ids:
            try:
                await bot.send_message(
                    chat_id,
                    "🤖 Бот запущен. Нажмите /start, чтобы открыть кнопки.",
                    reply_markup=main_keyboard(),
                )
            except Exception as exc:
                logger.warning("Не удалось написать пользователю %s: %s", chat_id, exc)
        await dispatcher.start_polling(bot)
    finally:
        for task in monitor_tasks:
            task.cancel()
        await asyncio.gather(*monitor_tasks, return_exceptions=True)
        await asyncio.gather(*(exchange.close() for exchange in exchanges.values()), return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception:
        logger.exception("Критическая ошибка")
        raise
