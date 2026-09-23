"""Main WordPress-inspired administration window."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEasingCurve, QDateTime, QThread, QTimer, Qt, QVariantAnimation, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFormLayout,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from api.instagram_publisher import InstagramPostData, InstagramPublisher
from api.max_publisher import MaxPostData, MaxPublisher
from api.telegram_publisher import TelegramPostData, TelegramPublisher
from api.vk_publisher import VKPostData, VKPublisher
from config.settings import Settings
from models.post import PostRecord, PostRepository
from ui.widgets.media_uploader import MediaUploader
from ui.widgets.platform_selector import PlatformSelector
from ui.widgets.publish_button import PublishButton
from ui.widgets.text_editor import TextEditor
from utils.auth import SecretStore, extract_vk_access_token, vk_oauth_url
from utils.network import resolve_network

import httpx

LOGGER = logging.getLogger(__name__)
PLATFORM_NAMES = {
    "vk": "ВКонтакте",
    "instagram": "Instagram",
    "telegram": "Telegram",
    "max": "MAX",
}


class PublishThread(QThread):
    platform_done = Signal(str, bool, str)
    all_done = Signal(dict)

    def __init__(
        self,
        text: str,
        media_paths: list[str],
        platforms: list[str],
        secrets: dict[str, dict[str, str]],
        settings: Settings,
    ) -> None:
        super().__init__()
        self.text = text
        self.media_paths = media_paths
        self.platforms = platforms
        self.secrets = secrets
        self.settings = settings

    def run(self) -> None:
        try:
            results = asyncio.run(self._publish_all())
        except Exception as exc:
            LOGGER.exception("Publishing worker failed")
            results = {"application": {"success": False, "error": str(exc)}}
        self.all_done.emit(results)

    async def _publish_all(self) -> dict[str, dict[str, Any]]:
        jobs = [self._publish_one(platform) for platform in self.platforms]
        pairs = await asyncio.gather(*jobs)
        return dict(pairs)

    async def _publish_one(self, platform: str) -> tuple[str, dict[str, Any]]:
        publisher = None
        try:
            publisher, post = self._build(platform)
            response = await publisher.publish(post)
            url = self._result_url(platform, response)
            result: dict[str, Any] = {"success": True, "url": url, "error": ""}
            self.platform_done.emit(platform, True, "Опубликовано")
        except Exception as exc:
            LOGGER.exception("Publishing to %s failed", platform)
            result = {"success": False, "url": "", "error": str(exc)}
            self.platform_done.emit(platform, False, str(exc))
        finally:
            if publisher is not None:
                await publisher.aclose()
        return platform, result

    def _build(self, platform: str):  # type: ignore[no-untyped-def]
        network = resolve_network(self.settings.network_mode, self.settings.proxy_url)
        if platform == "vk":
            values = self.secrets["vk"]
            group_id = int(values["group_id"]) if values["group_id"].strip() else None
            return VKPublisher(
                values["access_token"],
                group_id=group_id,
                proxy=network.proxy,
                trust_env=network.trust_env,
            ), VKPostData(
                text=self.text, media=self.media_paths
            )
        if platform == "instagram":
            values = self.secrets["instagram"]
            return InstagramPublisher(
                values["access_token"],
                values["account_id"],
                graph_version=self.settings.graph_version,
                public_media_base_url=self.settings.public_media_base_url or None,
                media_port=self.settings.media_port,
                proxy=network.proxy,
                trust_env=network.trust_env,
            ), InstagramPostData(text=self.text, media=self.media_paths)
        if platform == "telegram":
            values = self.secrets["telegram"]
            return TelegramPublisher(
                values["bot_token"],
                values["chat_id"],
                proxy=network.proxy,
                trust_env=network.trust_env,
            ), TelegramPostData(
                text=self.text, media=self.media_paths
            )
        if platform == "max":
            values = self.secrets["max"]
            return MaxPublisher(
                values["bot_token"],
                values["chat_id"],
                proxy=network.proxy,
                trust_env=network.trust_env,
            ), MaxPostData(text=self.text, media=self.media_paths)
        raise ValueError(f"Неизвестная платформа: {platform}")

    @staticmethod
    def _result_url(platform: str, response: Any) -> str:
        if not isinstance(response, dict):
            return ""
        if platform == "instagram" and response.get("id"):
            return f"https://www.instagram.com/p/{response['id']}"
        if platform == "max":
            message = response.get("message")
            if isinstance(message, dict):
                return str(message.get("link") or "")
        return str(response.get("url", ""))


class AuthCheckThread(QThread):
    done = Signal(str, bool, str)

    def __init__(self, platform: str, secrets: dict[str, dict[str, str]], settings: Settings) -> None:
        super().__init__()
        self.platform = platform
        self.secrets = secrets
        self.settings = settings

    def run(self) -> None:
        asyncio.run(self._check())

    async def _check(self) -> None:
        publisher = None
        try:
            network = resolve_network(self.settings.network_mode, self.settings.proxy_url)
            if self.platform == "vk":
                group_text = self.secrets["vk"]["group_id"].strip()
                publisher = VKPublisher(
                    self.secrets["vk"]["access_token"],
                    group_id=int(group_text) if group_text else None,
                    proxy=network.proxy,
                    trust_env=network.trust_env,
                )
            elif self.platform == "instagram":
                values = self.secrets["instagram"]
                publisher = InstagramPublisher(
                    values["access_token"],
                    values["account_id"],
                    graph_version=self.settings.graph_version,
                    proxy=network.proxy,
                    trust_env=network.trust_env,
                )
            elif self.platform == "telegram":
                values = self.secrets["telegram"]
                publisher = TelegramPublisher(
                    values["bot_token"],
                    values["chat_id"],
                    proxy=network.proxy,
                    trust_env=network.trust_env,
                )
            elif self.platform == "max":
                values = self.secrets["max"]
                publisher = MaxPublisher(
                    values["bot_token"],
                    values["chat_id"],
                    proxy=network.proxy,
                    trust_env=network.trust_env,
                )
            else:
                raise ValueError(f"Неизвестная платформа: {self.platform}")
            account = await publisher.authenticate()
            identity = account.get("username") or account.get("first_name") or account.get("id")
            self.done.emit(self.platform, True, f"Соединение установлено: {identity}")
        except Exception as exc:
            LOGGER.exception("Authentication check failed for %s", self.platform)
            self.done.emit(self.platform, False, str(exc))
        finally:
            if publisher is not None:
                await publisher.aclose()


class NetworkCheckThread(QThread):
    done = Signal(bool, str)

    ENDPOINTS = {
        "VK": "https://api.vk.com/",
        "Instagram": "https://graph.facebook.com/",
        "Telegram": "https://api.telegram.org/",
        "MAX": "https://platform-api2.max.ru/",
    }

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

    def run(self) -> None:
        try:
            report = asyncio.run(self._check())
            self.done.emit(all(success for success, _ in report.values()), self._format(report))
        except Exception as exc:
            self.done.emit(False, str(exc))

    async def _check(self) -> dict[str, tuple[bool, str]]:
        network = resolve_network(self.settings.network_mode, self.settings.proxy_url)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12), proxy=network.proxy, trust_env=network.trust_env
        ) as client:
            async def probe(name: str, url: str) -> tuple[str, tuple[bool, str]]:
                started = time.monotonic()
                try:
                    response = await client.get(url, follow_redirects=False)
                    elapsed = time.monotonic() - started
                    return name, (True, f"доступен, HTTP {response.status_code}, {elapsed:.1f} с")
                except httpx.RequestError as exc:
                    return name, (False, f"недоступен: {type(exc).__name__}")

            return dict(await asyncio.gather(*(probe(name, url) for name, url in self.ENDPOINTS.items())))

    def _format(self, report: dict[str, tuple[bool, str]]) -> str:
        network = resolve_network(self.settings.network_mode, self.settings.proxy_url)
        lines = [network.description, ""]
        lines.extend(f"{name}: {message}" for name, (_, message) in report.items())
        return "\n".join(lines)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings.load()
        self.secret_store = SecretStore()
        self.secrets = self.secret_store.load()
        self.repository = PostRepository()
        self._threads: set[QThread] = set()
        self._active_publish: PublishThread | None = None
        self._active_post_id: int | None = None
        self._bulk_posts: list[dict[str, Any]] = []
        self._row_animations: set[QVariantAnimation] = set()

        self.setWindowTitle("AutoPoster")
        self.resize(1200, 800)
        self.setMinimumSize(900, 600)
        self._build_ui()
        self.refresh_history()
        self.refresh_queue()

        self.queue_timer = QTimer(self)
        self.queue_timer.setInterval(15_000)
        self.queue_timer.timeout.connect(self._process_queue)
        self.queue_timer.start()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("topBar")
        header_layout = QHBoxLayout(header)
        logo = QLabel("AutoPoster")
        logo.setObjectName("logo")
        settings_button = QPushButton("Настройки")
        settings_button.setObjectName("topButton")
        settings_button.clicked.connect(lambda: self._show_page(2))
        site_button = QPushButton("Открыть сайт")
        site_button.setObjectName("topButton")
        site_button.clicked.connect(self._open_public_site)
        header_layout.addWidget(logo)
        header_layout.addStretch()
        header_layout.addWidget(site_button)
        header_layout.addWidget(settings_button)
        root_layout.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.navigation = QListWidget()
        self.navigation.setObjectName("sidebar")
        self.navigation.setFixedWidth(230)
        self.navigation.addItems(
            [
                "Создать пост",
                "История",
                "Настройки аккаунтов",
                "Очередь публикаций",
                "Массовая загрузка",
            ]
        )
        self.navigation.currentRowChanged.connect(self._show_page)
        body.addWidget(self.navigation)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._create_post_page())
        self.pages.addWidget(self._create_history_page())
        self.pages.addWidget(self._create_settings_page())
        self.pages.addWidget(self._create_queue_page())
        self.pages.addWidget(self._create_bulk_page())
        body.addWidget(self.pages, 1)
        root_layout.addLayout(body, 1)
        self.setCentralWidget(root)
        self.navigation.setCurrentRow(0)

    def _page(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        return page, layout

    def _create_post_page(self) -> QWidget:
        page, layout = self._page("Создать публикацию")
        card = QFrame()
        card.setObjectName("card")
        form = QVBoxLayout(card)
        form.addWidget(QLabel("Текст"))
        self.text_editor = TextEditor()
        form.addWidget(self.text_editor)
        form.addWidget(QLabel("Медиафайлы (до 10)"))
        self.media_uploader = MediaUploader()
        form.addWidget(self.media_uploader)
        form.addWidget(QLabel("Платформы"))
        self.platform_selector = PlatformSelector()
        form.addWidget(self.platform_selector)

        schedule_row = QHBoxLayout()
        self.schedule_enabled = QCheckBox("Отложить публикацию")
        self.schedule_at = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        self.schedule_at.setCalendarPopup(True)
        self.schedule_at.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.schedule_at.setEnabled(False)
        self.schedule_enabled.toggled.connect(self.schedule_at.setEnabled)
        schedule_row.addWidget(self.schedule_enabled)
        schedule_row.addWidget(self.schedule_at)
        schedule_row.addStretch()
        form.addLayout(schedule_row)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        form.addWidget(self.progress)
        form.addWidget(self.result_label)

        buttons = QHBoxLayout()
        self.publish_button = PublishButton()
        self.publish_button.clicked.connect(self._publish_clicked)
        draft = QPushButton("Сохранить в черновики")
        draft.clicked.connect(self._save_draft)
        buttons.addWidget(self.publish_button)
        buttons.addWidget(draft)
        buttons.addStretch()
        form.addLayout(buttons)
        layout.addWidget(card)
        layout.addStretch()
        return page

    def _create_history_page(self) -> QWidget:
        page, layout = self._page("История публикаций")
        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(["Дата", "Текст", "Платформы", "Статус", ""])
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.history_table)
        return page

    def _create_queue_page(self) -> QWidget:
        page, layout = self._page("Очередь публикаций")
        self.queue_table = QTableWidget(0, 3)
        self.queue_table.setHorizontalHeaderLabels(["Время", "Текст", "Платформы"])
        self.queue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.queue_table)
        return page

    def _create_bulk_page(self) -> QWidget:
        page, layout = self._page("Массовая загрузка постов")
        hint = QLabel(
            "CSV: text, media_paths, platforms, scheduled_at. Списки разделяются точкой с запятой.\n"
            "JSON: массив объектов с теми же полями. Время — ISO 8601, например 2026-09-24T12:30."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        actions = QHBoxLayout()
        choose = QPushButton("Загрузить CSV или JSON")
        choose.clicked.connect(self._load_bulk_file)
        enqueue = QPushButton("Добавить всё в очередь")
        enqueue.setObjectName("primaryButton")
        enqueue.clicked.connect(self._enqueue_bulk_posts)
        clear = QPushButton("Очистить")
        clear.clicked.connect(self._clear_bulk_posts)
        actions.addWidget(choose)
        actions.addWidget(enqueue)
        actions.addWidget(clear)
        actions.addStretch()
        layout.addLayout(actions)

        self.bulk_table = QTableWidget(0, 4)
        self.bulk_table.setHorizontalHeaderLabels(["Текст", "Медиа", "Платформы", "Время"])
        self.bulk_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.bulk_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.bulk_table)
        return page

    def _create_settings_page(self) -> QWidget:
        page, layout = self._page("Настройки аккаунтов")
        tabs = QTabWidget()
        self.fields: dict[str, dict[str, QLineEdit]] = {}
        definitions = {
            "vk": [
                ("client_id", "ID standalone-приложения"),
                ("access_token", "Личный access token"),
                ("group_id", "ID группы (без минуса)"),
            ],
            "instagram": [
                ("app_id", "Facebook App ID"),
                ("app_secret", "Facebook App Secret"),
                ("access_token", "Long-lived access token"),
                ("account_id", "Instagram Business Account ID"),
            ],
            "telegram": [("bot_token", "Bot token"), ("chat_id", "Channel chat_id")],
            "max": [("bot_token", "Токен бота MAX"), ("chat_id", "chat_id канала или чата")],
        }
        for platform, field_defs in definitions.items():
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            form = QFormLayout()
            self.fields[platform] = {}
            for key, label in field_defs:
                field = QLineEdit(self.secrets[platform].get(key, ""))
                if "token" in key or "secret" in key:
                    field.setEchoMode(QLineEdit.EchoMode.Password)
                self.fields[platform][key] = field
                form.addRow(label, field)
            tab_layout.addLayout(form)
            actions = QHBoxLayout()
            check = QPushButton("Проверить соединение")
            check.clicked.connect(lambda checked=False, name=platform: self._check_connection(name))
            actions.addWidget(check)
            if platform == "vk":
                oauth = QPushButton("Получить токен")
                oauth.clicked.connect(self._open_vk_oauth)
                actions.addWidget(oauth)
                paste_token = QPushButton("Вставить личный токен")
                paste_token.clicked.connect(self._paste_vk_token)
                actions.addWidget(paste_token)
            actions.addStretch()
            tab_layout.addLayout(actions)
            tab_layout.addStretch()
            tabs.addTab(tab, PLATFORM_NAMES[platform])
        layout.addWidget(tabs)

        network = QFrame()
        network.setObjectName("card")
        network_form = QFormLayout(network)
        self.network_mode = QComboBox()
        self.network_mode.addItem("Системные настройки", "system")
        self.network_mode.addItem("Прямое подключение", "direct")
        self.network_mode.addItem("Свой прокси", "proxy")
        mode_index = self.network_mode.findData(self.settings.network_mode)
        self.network_mode.setCurrentIndex(max(0, mode_index))
        self.proxy_field = QLineEdit(self.settings.proxy_url)
        self.proxy_field.setPlaceholderText("http://user:password@host:port или socks5://host:port")
        self.public_url_field = QLineEdit(self.settings.public_media_base_url)
        self.public_url_field.setPlaceholderText("https://публичный-туннель.example")
        self.public_site_field = QLineEdit(self.settings.public_site_url)
        self.public_site_field.setPlaceholderText("https://autoposter.example")
        network_form.addRow("Режим сети", self.network_mode)
        network_form.addRow("Адрес прокси", self.proxy_field)
        network_form.addRow("Публичный URL медиа для Instagram", self.public_url_field)
        network_form.addRow("Публичный адрес сайта", self.public_site_field)
        self.network_mode.currentIndexChanged.connect(self._network_mode_changed)
        self._network_mode_changed()
        layout.addWidget(network)
        test_network = QPushButton("Проверить сеть")
        test_network.clicked.connect(self._check_network)
        layout.addWidget(test_network, alignment=Qt.AlignmentFlag.AlignLeft)
        save = QPushButton("Сохранить настройки")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save_settings)
        layout.addWidget(save, alignment=Qt.AlignmentFlag.AlignLeft)
        return page

    def _show_page(self, index: int) -> None:
        if index < 0:
            return
        self.pages.setCurrentIndex(index)
        self.navigation.setCurrentRow(index)
        if index == 1:
            self.refresh_history()
        elif index == 3:
            self.refresh_queue()

    def _post_values(self) -> tuple[str, list[str], list[str]] | None:
        text = self.text_editor.plain_text()
        media = self.media_uploader.paths
        platforms = self.platform_selector.selected()
        if not text and not media:
            QMessageBox.warning(self, "Пустая публикация", "Добавьте текст или медиафайл.")
            return None
        if not platforms:
            QMessageBox.warning(self, "Платформы не выбраны", "Выберите хотя бы одну платформу.")
            return None
        return text, media, platforms

    def _publish_clicked(self) -> None:
        values = self._post_values()
        if values is None:
            return
        text, media, platforms = values
        if self.schedule_enabled.isChecked():
            scheduled = self.schedule_at.dateTime().toPython()
            if scheduled <= datetime.now():
                QMessageBox.warning(self, "Неверное время", "Выберите время в будущем.")
                return
            self.repository.create(
                text=text, media_paths=media, platforms=platforms, status="queued", scheduled_at=scheduled
            )
            self.result_label.setText("Публикация добавлена в очередь.")
            self.refresh_queue()
            return
        post_id = self.repository.create(
            text=text, media_paths=media, platforms=platforms, status="publishing"
        )
        self._start_publish(post_id, text, media, platforms)

    def _start_publish(self, post_id: int, text: str, media: list[str], platforms: list[str]) -> None:
        if self._active_publish and self._active_publish.isRunning():
            self.repository.update_result(
                post_id, "queued", {"application": {"success": False, "error": "Ожидание текущей публикации"}}
            )
            return
        self.secrets = self._collect_secrets()
        self._active_post_id = post_id
        self._active_publish = PublishThread(text, media, platforms, self.secrets, self.settings)
        self._active_publish.platform_done.connect(self._platform_done)
        self._active_publish.all_done.connect(self._publish_finished)
        self._track_thread(self._active_publish)
        self.progress.setRange(0, len(platforms))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.result_label.clear()
        self.publish_button.setEnabled(False)
        self._active_publish.start()

    def _platform_done(self, platform: str, success: bool, message: str) -> None:
        self.progress.setValue(self.progress.value() + 1)
        marker = "Успешно" if success else "Ошибка"
        self.result_label.setText(
            self.result_label.text() + f"{PLATFORM_NAMES.get(platform, platform)}: {marker} - {message}\n"
        )

    def _publish_finished(self, results: dict[str, dict[str, Any]]) -> None:
        status = "success" if results and all(item.get("success") for item in results.values()) else "error"
        if self._active_post_id is not None:
            self.repository.update_result(self._active_post_id, status, results)
        self.publish_button.setEnabled(True)
        self._active_post_id = None
        self._active_publish = None
        self.refresh_history()
        QTimer.singleShot(0, self._process_queue)

    def _save_draft(self) -> None:
        text = self.text_editor.plain_text()
        media = self.media_uploader.paths
        platforms = self.platform_selector.selected()
        self.repository.create(text=text, media_paths=media, platforms=platforms, status="draft")
        self.result_label.setText("Черновик сохранён.")
        self.refresh_history()

    def _save_settings(self, checked: bool = False, *, show_message: bool = True) -> None:
        self.secrets = self._collect_secrets()
        try:
            self.settings.network_mode = str(self.network_mode.currentData())
            self.settings.proxy_url = self.proxy_field.text().strip()
            resolve_network(self.settings.network_mode, self.settings.proxy_url)
        except ValueError as exc:
            QMessageBox.warning(self, "Настройки сети", str(exc))
            return
        self.secret_store.save(self.secrets)
        self.settings.public_media_base_url = self.public_url_field.text().strip()
        self.settings.public_site_url = self.public_site_field.text().strip()
        self.settings.save()
        if show_message:
            QMessageBox.information(self, "Настройки", "Настройки сохранены.")

    def _collect_secrets(self) -> dict[str, dict[str, str]]:
        return {
            platform: {key: field.text().strip() for key, field in fields.items()}
            for platform, fields in self.fields.items()
        }

    def _open_vk_oauth(self) -> None:
        client_id = self.fields["vk"]["client_id"].text().strip()
        if not client_id:
            QMessageBox.warning(self, "VK OAuth", "Сначала укажите ID приложения.")
            return
        webbrowser.open(vk_oauth_url(client_id))
        value, accepted = QInputDialog.getMultiLineText(
            self,
            "VK OAuth",
            "Разрешите доступ в браузере. Затем вставьте сюда полный адрес итоговой страницы\n"
            "из адресной строки (с #access_token=...) или только сам токен:",
        )
        if accepted:
            self._accept_vk_token(value)

    def _open_public_site(self) -> None:
        url = self.settings.public_site_url.strip()
        if not url:
            QMessageBox.information(
                self,
                "Сайт AutoPoster",
                "Укажите публичный адрес сайта в разделе настроек аккаунтов.",
            )
            self._show_page(2)
            return
        if not url.startswith(("https://", "http://")):
            url = "https://" + url
        webbrowser.open(url)

    def _paste_vk_token(self) -> None:
        value, accepted = QInputDialog.getMultiLineText(
            self,
            "Личный токен VK",
            "Вставьте личный access token или полный URL с #access_token=...:",
        )
        if accepted:
            self._accept_vk_token(value)

    def _accept_vk_token(self, value: str) -> None:
        try:
            token = extract_vk_access_token(value)
        except ValueError as exc:
            QMessageBox.warning(self, "VK OAuth", str(exc))
            return
        self.fields["vk"]["access_token"].setText(token)
        self._save_settings(show_message=False)
        self._check_connection("vk")

    def _network_mode_changed(self) -> None:
        self.proxy_field.setEnabled(self.network_mode.currentData() == "proxy")

    def _check_network(self) -> None:
        try:
            self.settings.network_mode = str(self.network_mode.currentData())
            self.settings.proxy_url = self.proxy_field.text().strip()
            resolve_network(self.settings.network_mode, self.settings.proxy_url)
        except ValueError as exc:
            QMessageBox.warning(self, "Проверка сети", str(exc))
            return
        thread = NetworkCheckThread(self.settings)
        thread.done.connect(self._network_checked)
        self._track_thread(thread)
        thread.start()

    def _network_checked(self, success: bool, report: str) -> None:
        if success:
            QMessageBox.information(self, "Проверка сети", report)
        else:
            QMessageBox.warning(self, "Проверка сети", report)

    def _check_connection(self, platform: str) -> None:
        self.secrets = self._collect_secrets()
        required = {
            "vk": ("access_token", "Введите или получите личный access token VK."),
            "instagram": ("access_token", "Введите long-lived access token Instagram."),
            "telegram": ("bot_token", "Введите токен Telegram-бота."),
            "max": ("bot_token", "Введите токен бота MAX."),
        }
        key, message = required[platform]
        if not self.secrets[platform][key]:
            QMessageBox.warning(self, f"Проверка: {PLATFORM_NAMES[platform]}", message)
            return
        if platform == "vk":
            group_id = self.secrets["vk"]["group_id"]
            if group_id and (not group_id.isdigit() or int(group_id) <= 0):
                QMessageBox.warning(
                    self, "Проверка: ВКонтакте", "ID группы должен быть положительным числом без минуса."
                )
                return
        if platform == "max":
            chat_id = self.secrets["max"]["chat_id"]
            if not chat_id:
                QMessageBox.warning(self, "Проверка: MAX", "Введите chat_id канала или чата MAX.")
                return
            try:
                int(chat_id)
            except ValueError:
                QMessageBox.warning(self, "Проверка: MAX", "chat_id MAX должен быть числом.")
                return
        thread = AuthCheckThread(platform, self.secrets, self.settings)
        thread.done.connect(self._connection_checked)
        self._track_thread(thread)
        thread.start()

    def _connection_checked(self, platform: str, success: bool, message: str) -> None:
        title = f"Проверка: {PLATFORM_NAMES[platform]}"
        if success:
            QMessageBox.information(self, title, message)
        else:
            QMessageBox.warning(self, title, f"Не удалось подключиться.\n{message}")

    def _track_thread(self, thread: QThread) -> None:
        self._threads.add(thread)
        thread.finished.connect(lambda: self._threads.discard(thread))
        thread.finished.connect(thread.deleteLater)

    def _process_queue(self) -> None:
        if self._active_publish and self._active_publish.isRunning():
            return
        due = self.repository.due(datetime.now())
        if due:
            record = due[0]
            self._start_publish(record.id, record.text, record.media_paths, record.platforms)
        self.refresh_queue()

    def refresh_history(self) -> None:
        records = self.repository.recent()
        self.history_table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = [
                record.created_at.strftime("%d.%m.%Y %H:%M"),
                _preview(record.text),
                ", ".join(PLATFORM_NAMES.get(item, item) for item in record.platforms),
                _status_label(record.status),
            ]
            for column, value in enumerate(values):
                self.history_table.setItem(row, column, QTableWidgetItem(value))
            remove = QPushButton("Удалить")
            remove.setProperty("postId", record.id)
            remove.clicked.connect(self._delete_history_post)
            self.history_table.setCellWidget(row, 4, remove)

    def refresh_queue(self) -> None:
        records = self.repository.queued()
        self.queue_table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = [
                record.scheduled_at.strftime("%d.%m.%Y %H:%M") if record.scheduled_at else "При первой возможности",
                _preview(record.text),
                ", ".join(PLATFORM_NAMES.get(item, item) for item in record.platforms),
            ]
            for column, value in enumerate(values):
                self.queue_table.setItem(row, column, QTableWidgetItem(value))

    def _delete_history_post(self) -> None:
        button = self.sender()
        post_id = int(button.property("postId"))
        answer = QMessageBox.question(
            self,
            "Удалить публикацию",
            "Удалить запись из истории? Опубликованный пост в социальной сети удалён не будет.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        row = next(
            (index for index in range(self.history_table.rowCount()) if self.history_table.cellWidget(index, 4) is button),
            -1,
        )
        if row < 0:
            self.repository.delete(post_id)
            self.refresh_history()
            return
        start_height = self.history_table.rowHeight(row)
        animation = QVariantAnimation(self)
        animation.setDuration(260)
        animation.setStartValue(start_height)
        animation.setEndValue(0)
        animation.setEasingCurve(QEasingCurve.Type.InCubic)
        animation.valueChanged.connect(lambda value, target=row: self.history_table.setRowHeight(target, value))
        animation.finished.connect(lambda: self._finish_history_delete(post_id, animation))
        self._row_animations.add(animation)
        animation.start()

    def _finish_history_delete(self, post_id: int, animation: QVariantAnimation) -> None:
        self.repository.delete(post_id)
        self._row_animations.discard(animation)
        animation.deleteLater()
        self.refresh_history()
        self.refresh_queue()

    def _load_bulk_file(self) -> None:
        path_text, _ = QFileDialog.getOpenFileName(
            self, "Загрузить посты", "", "Посты (*.csv *.json)"
        )
        if not path_text:
            return
        try:
            rows = _read_bulk_posts(Path(path_text))
            self._bulk_posts = [_normalize_bulk_post(item) for item in rows]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Массовая загрузка", str(exc))
            return
        self._refresh_bulk_table()

    def _refresh_bulk_table(self) -> None:
        self.bulk_table.setRowCount(len(self._bulk_posts))
        for row, post in enumerate(self._bulk_posts):
            values = [
                _preview(post["text"]),
                "; ".join(post["media_paths"]),
                ", ".join(PLATFORM_NAMES[name] for name in post["platforms"]),
                post["scheduled_at"].strftime("%d.%m.%Y %H:%M"),
            ]
            for column, value in enumerate(values):
                self.bulk_table.setItem(row, column, QTableWidgetItem(value))

    def _clear_bulk_posts(self) -> None:
        self._bulk_posts.clear()
        self._refresh_bulk_table()

    def _enqueue_bulk_posts(self) -> None:
        if not self._bulk_posts:
            QMessageBox.information(self, "Массовая загрузка", "Сначала загрузите CSV или JSON.")
            return
        for post in self._bulk_posts:
            self.repository.create(
                text=post["text"],
                media_paths=post["media_paths"],
                platforms=post["platforms"],
                status="queued",
                scheduled_at=post["scheduled_at"],
            )
        count = len(self._bulk_posts)
        self._clear_bulk_posts()
        self.refresh_queue()
        QMessageBox.information(self, "Массовая загрузка", f"В очередь добавлено постов: {count}.")


def _preview(text: str, limit: int = 90) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "..."


def _status_label(status: str) -> str:
    return {
        "draft": "Черновик",
        "queued": "В очереди",
        "publishing": "Публикуется",
        "success": "Успешно",
        "error": "Ошибка",
    }.get(status, status)


def _read_bulk_posts(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ValueError("JSON должен содержать массив объектов")
        return data
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))
    raise ValueError("Поддерживаются только CSV и JSON")


def _normalize_bulk_post(item: dict[str, Any]) -> dict[str, Any]:
    text = str(item.get("text") or "").strip()
    raw_media = item.get("media_paths") or []
    media_paths = (
        [part.strip() for part in raw_media.replace(",", ";").split(";") if part.strip()]
        if isinstance(raw_media, str)
        else [str(part).strip() for part in raw_media if str(part).strip()]
    )
    raw_platforms = item.get("platforms") or []
    platforms = (
        [part.strip().lower() for part in raw_platforms.replace(",", ";").split(";") if part.strip()]
        if isinstance(raw_platforms, str)
        else [str(part).strip().lower() for part in raw_platforms if str(part).strip()]
    )
    unknown = [name for name in platforms if name not in PLATFORM_NAMES]
    if unknown:
        raise ValueError("Неизвестные платформы: " + ", ".join(unknown))
    if not platforms:
        raise ValueError("У каждого поста должна быть указана хотя бы одна платформа")
    if not text and not media_paths:
        raise ValueError("Пост не может быть пустым")
    if len(media_paths) > 10:
        raise ValueError("В одном посте допускается не более 10 медиафайлов")
    raw_time = str(item.get("scheduled_at") or "").strip()
    try:
        scheduled_at = datetime.fromisoformat(raw_time) if raw_time else datetime.now()
    except ValueError as exc:
        raise ValueError(f"Некорректное время scheduled_at: {raw_time}") from exc
    if scheduled_at < datetime.now():
        scheduled_at = datetime.now()
    return {
        "text": text,
        "media_paths": media_paths,
        "platforms": list(dict.fromkeys(platforms)),
        "scheduled_at": scheduled_at,
    }
