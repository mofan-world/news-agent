from __future__ import annotations

import hashlib
import hmac
import html
import http.cookies
import json
import logging
import os
import re
import secrets
import smtplib
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_GLOBAL_FEEDS = [
    "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    "https://news.google.com/rss/search?q=%E5%85%A8%E7%90%83%20%E7%83%AD%E7%82%B9%20when%3A1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    "https://news.google.com/rss/search?q=international%20news%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
]

DEFAULT_TECH_FEEDS = [
    "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    "https://news.google.com/rss/search?q=%E7%A7%91%E6%8A%80%20when%3A1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    "https://news.google.com/rss/search?q=technology%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
]

DEFAULT_USER_AGENT = "news-agent/1.0 (+https://github.com/local/news-agent)"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123456"
SESSION_COOKIE = "news_agent_session"
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_FORM_BYTES = 256 * 1024
MAX_ARTICLE_BYTES = 512 * 1024
MAX_TRANSLATION_BYTES = 512 * 1024
MAX_TRANSLATION_CHARS = 900


@dataclass(frozen=True)
class NewsItem:
    title: str
    summary: str
    link: str
    source: str
    published: str
    image_url: str = ""
    title_en: str = ""
    summary_en: str = ""


@dataclass(frozen=True)
class FeedError:
    url: str
    message: str


class OpenAINewsError(ValueError):
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class OpenSourceNewsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    enabled: bool
    use_openai_news: bool
    openai_api_key: str
    openai_model: str
    openai_base_url: str
    openai_web_search_tool: str
    use_open_source_news: bool
    open_source_provider: str
    open_source_base_url: str
    open_source_model: str
    open_source_api_key: str
    open_source_candidate_count: int
    email_to: list[str]
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_ssl: bool
    smtp_starttls: bool
    schedule_hour: int
    schedule_minute: int
    schedule_interval_minutes: int
    timezone: ZoneInfo
    timezone_name: str
    global_feeds: list[str]
    tech_feeds: list[str]
    top_n: int
    request_timeout: int
    user_agent: str
    bilingual: bool
    include_images: bool
    fetch_article_images: bool
    dry_run: bool


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def get_text(self) -> str:
        return " ".join(self.parts)


class ImageExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()
        if tag == "img":
            src = attr.get("src") or attr.get("data-src") or attr.get("data-original")
            if src:
                self.images.append(src)
        elif tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").lower()
            if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
                content = attr.get("content")
                if content:
                    self.images.append(content)
        elif tag == "link":
            rel = attr.get("rel", "").lower()
            if "image_src" in rel and attr.get("href"):
                self.images.append(attr["href"])


def parse_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw.strip())
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def parse_list(raw: str | None, defaults: list[str] | None = None) -> list[str]:
    if not raw or not raw.strip():
        return list(defaults or [])
    values = [
        part.strip()
        for part in re.split(r"[\n,]+", raw)
        if part.strip()
    ]
    return values or list(defaults or [])


def parse_bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_schedule_time(raw: str, field_name: str = "schedule_start_time") -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", raw or "")
    if not match:
        raise ValueError(f"{field_name} must use HH:MM format, for example 08:30")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"{field_name} is outside the valid 00:00-23:59 range")
    return hour, minute


def hash_password(password: str) -> str:
    iterations = 260_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            int(iterations_raw),
        ).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def default_config() -> dict[str, Any]:
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip() or smtp_username
    return {
        "schema_version": 2,
        "admin_username": os.getenv("ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME).strip()
        or DEFAULT_ADMIN_USERNAME,
        "admin_password_hash": hash_password(
            os.getenv("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
        ),
        "admin_password_changed": False,
        "session_secret": secrets.token_urlsafe(32),
        "enabled": True,
        "use_openai_news": parse_env_bool("USE_OPENAI_NEWS", True),
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "openai_model": os.getenv("OPENAI_MODEL", "gpt-4.1").strip() or "gpt-4.1",
        "openai_base_url": os.getenv(
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
        ).rstrip("/"),
        "openai_web_search_tool": os.getenv(
            "OPENAI_WEB_SEARCH_TOOL",
            "web_search",
        ).strip() or "web_search",
        "use_open_source_news": parse_env_bool("USE_OPEN_SOURCE_NEWS", True),
        "open_source_provider": os.getenv(
            "OPEN_SOURCE_PROVIDER",
            "ollama",
        ).strip() or "ollama",
        "open_source_base_url": os.getenv(
            "OPEN_SOURCE_BASE_URL",
            "http://host.docker.internal:11434",
        ).rstrip("/"),
        "open_source_model": os.getenv(
            "OPEN_SOURCE_MODEL",
            "qwen2.5:7b",
        ).strip() or "qwen2.5:7b",
        "open_source_api_key": os.getenv("OPEN_SOURCE_API_KEY", ""),
        "open_source_candidate_count": parse_env_int(
            "OPEN_SOURCE_CANDIDATE_COUNT",
            30,
            minimum=10,
        ),
        "timezone_name": os.getenv("TZ", "Asia/Shanghai").strip() or "Asia/Shanghai",
        "schedule_start_time": os.getenv("SCHEDULE_TIME", "08:30").strip() or "08:30",
        "schedule_interval_minutes": parse_env_int(
            "SCHEDULE_INTERVAL_MINUTES",
            1440,
            minimum=1,
        ),
        "email_to": parse_list(os.getenv("EMAIL_TO"), ["swh_2018@126.com"]),
        "smtp_host": os.getenv("SMTP_HOST", "").strip(),
        "smtp_port": parse_env_int("SMTP_PORT", 465, minimum=1),
        "smtp_username": smtp_username,
        "smtp_password": os.getenv("SMTP_PASSWORD", ""),
        "smtp_from": smtp_from,
        "smtp_ssl": parse_env_bool("SMTP_SSL", True),
        "smtp_starttls": parse_env_bool("SMTP_STARTTLS", False),
        "global_feeds": parse_list(os.getenv("GLOBAL_FEEDS"), DEFAULT_GLOBAL_FEEDS),
        "tech_feeds": parse_list(os.getenv("TECH_FEEDS"), DEFAULT_TECH_FEEDS),
        "top_n": parse_env_int("TOP_N", 10, minimum=1),
        "request_timeout": parse_env_int("REQUEST_TIMEOUT", 20, minimum=1),
        "user_agent": os.getenv("USER_AGENT", DEFAULT_USER_AGENT).strip()
        or DEFAULT_USER_AGENT,
        "bilingual": parse_env_bool("BILINGUAL_EMAIL", True),
        "include_images": parse_env_bool("INCLUDE_IMAGES", True),
        "fetch_article_images": parse_env_bool("FETCH_ARTICLE_IMAGES", True),
        "dry_run": parse_env_bool("DRY_RUN", False),
    }


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in merged:
            merged[key] = value
    return merged


class ConfigStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self.lock:
            defaults = default_config()
            if not self.path.exists():
                self.save(defaults)
                return defaults

            with self.path.open("r", encoding="utf-8") as config_file:
                stored = json.load(config_file)
            config = merge_config(defaults, stored)
            if config != stored:
                self.save(config)
            return config

    def save(self, config: dict[str, Any]) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as config_file:
                json.dump(config, config_file, ensure_ascii=False, indent=2)
                config_file.write("\n")
            os.replace(tmp_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def load_settings(self) -> Settings:
        return settings_from_config(self.load())


def settings_from_config(config: dict[str, Any]) -> Settings:
    timezone_name = str(config.get("timezone_name", "Asia/Shanghai")).strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc

    schedule_hour, schedule_minute = parse_schedule_time(
        str(config.get("schedule_start_time", "08:30")),
        "schedule_start_time",
    )
    interval_minutes = int(config.get("schedule_interval_minutes", 1440))
    if interval_minutes < 1:
        raise ValueError("schedule_interval_minutes must be >= 1")

    top_n = int(config.get("top_n", 10))
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if top_n > 50:
        raise ValueError("top_n must be <= 50")

    request_timeout = int(config.get("request_timeout", 20))
    if request_timeout < 1:
        raise ValueError("request_timeout must be >= 1")

    smtp_port = int(config.get("smtp_port", 465))
    if smtp_port < 1:
        raise ValueError("smtp_port must be >= 1")

    return Settings(
        enabled=parse_bool_value(config.get("enabled"), True),
        use_openai_news=parse_bool_value(config.get("use_openai_news"), True),
        openai_api_key=str(config.get("openai_api_key", "")),
        openai_model=str(config.get("openai_model", "gpt-4.1")).strip() or "gpt-4.1",
        openai_base_url=str(
            config.get("openai_base_url", "https://api.openai.com/v1")
        ).strip().rstrip("/") or "https://api.openai.com/v1",
        openai_web_search_tool=str(
            config.get("openai_web_search_tool", "web_search")
        ).strip() or "web_search",
        use_open_source_news=parse_bool_value(config.get("use_open_source_news"), True),
        open_source_provider=str(config.get("open_source_provider", "ollama")).strip()
        or "ollama",
        open_source_base_url=str(
            config.get("open_source_base_url", "http://host.docker.internal:11434")
        ).strip().rstrip("/") or "http://host.docker.internal:11434",
        open_source_model=str(config.get("open_source_model", "qwen2.5:7b")).strip()
        or "qwen2.5:7b",
        open_source_api_key=str(config.get("open_source_api_key", "")),
        open_source_candidate_count=min(100, max(
            10,
            int(config.get("open_source_candidate_count", 30)),
        )),
        email_to=list(config.get("email_to") or []),
        smtp_host=str(config.get("smtp_host", "")).strip(),
        smtp_port=smtp_port,
        smtp_username=str(config.get("smtp_username", "")).strip(),
        smtp_password=str(config.get("smtp_password", "")),
        smtp_from=str(config.get("smtp_from", "")).strip()
        or str(config.get("smtp_username", "")).strip(),
        smtp_ssl=parse_bool_value(config.get("smtp_ssl"), True),
        smtp_starttls=parse_bool_value(config.get("smtp_starttls"), False),
        schedule_hour=schedule_hour,
        schedule_minute=schedule_minute,
        schedule_interval_minutes=interval_minutes,
        timezone=timezone,
        timezone_name=timezone_name,
        global_feeds=list(config.get("global_feeds") or []),
        tech_feeds=list(config.get("tech_feeds") or []),
        top_n=top_n,
        request_timeout=request_timeout,
        user_agent=str(config.get("user_agent", DEFAULT_USER_AGENT)).strip()
        or DEFAULT_USER_AGENT,
        bilingual=parse_bool_value(config.get("bilingual"), True),
        include_images=parse_bool_value(config.get("include_images"), True),
        fetch_article_images=parse_bool_value(config.get("fetch_article_images"), True),
        dry_run=parse_bool_value(config.get("dry_run"), False),
    )


def validate_settings(settings: Settings) -> None:
    if not settings.email_to:
        raise ValueError("请至少配置一个收件邮箱。")
    if settings.use_open_source_news:
        if settings.open_source_provider not in {"ollama", "openai_compatible"}:
            raise ValueError("开源大模型 Provider 只能是 ollama 或 openai_compatible。")
        if not settings.open_source_base_url:
            raise ValueError("缺少开源大模型 Base URL。")
        if not settings.open_source_model:
            raise ValueError("缺少开源大模型名称。")
    if not settings.global_feeds:
        raise ValueError("请至少配置一个全球热点 RSS 新闻源。")
    if not settings.tech_feeds:
        raise ValueError("请至少配置一个科技新闻 RSS 新闻源。")
    if settings.dry_run:
        return

    missing = []
    if not settings.smtp_host:
        missing.append("SMTP 服务器")
    if not settings.smtp_from:
        missing.append("发件邮箱")
    if not settings.smtp_username:
        missing.append("SMTP 账号")
    if not settings.smtp_password:
        missing.append("SMTP 密码/授权码")
    if missing:
        raise ValueError("缺少邮件配置：" + "、".join(missing))


def html_to_text(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value)
    parser.close()
    text = parser.get_text() or value
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def shorten(value: str, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip(" ,.;，。；") + "…"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text(element: ET.Element, names: Iterable[str]) -> str:
    wanted = set(names)
    for child in element:
        if local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def child_attr(element: ET.Element, child_name: str, attr_name: str) -> str:
    for child in element:
        if local_name(child.tag) == child_name:
            value = child.attrib.get(attr_name)
            if value:
                return value.strip()
    return ""


def source_from_link(link: str) -> str:
    try:
        hostname = urllib.parse.urlparse(link).hostname or ""
    except ValueError:
        return ""
    return hostname.removeprefix("www.")


def is_unavailable_feed_item(title: str, summary: str, link: str) -> bool:
    text = f"{title} {summary}".lower()
    unavailable_markers = [
        "this feed is not available",
        "feed is not available",
        "该提要不可用",
        "该摘要不可用",
        "此供稿不可用",
        "访问 google 新闻",
        "visit google news",
    ]
    if any(marker in text for marker in unavailable_markers):
        host = source_from_link(link)
        return not host or host == "news.google.com"
    return False


def normalize_asset_url(value: str, base_url: str) -> str:
    value = html.unescape(value or "").strip()
    if not value or value.startswith("data:"):
        return ""
    if value.startswith("//"):
        scheme = urllib.parse.urlparse(base_url).scheme or "https"
        value = f"{scheme}:{value}"
    value = urllib.parse.urljoin(base_url, value)
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    return value


def first_image_url(candidates: Iterable[str], base_url: str) -> str:
    for candidate in candidates:
        image_url = normalize_asset_url(candidate, base_url)
        if image_url:
            return image_url
    return ""


def extract_images_from_html(value: str) -> list[str]:
    parser = ImageExtractor()
    try:
        parser.feed(value or "")
        parser.close()
    except Exception:
        return []
    return parser.images


def element_attrs(element: ET.Element) -> dict[str, str]:
    return {
        local_name(key).lower(): value
        for key, value in element.attrib.items()
    }


def extract_rss_image_url(
    entry: ET.Element,
    raw_summary: str,
    base_url: str,
) -> str:
    candidates: list[str] = []
    for child in entry.iter():
        name = local_name(child.tag).lower()
        attrs = element_attrs(child)
        url = attrs.get("url") or attrs.get("href")
        media_type = attrs.get("type", "").lower()

        if name in {"content", "thumbnail"} and url:
            candidates.append(url)
        elif name == "enclosure" and url:
            if media_type.startswith("image/") or re.search(
                r"\.(?:jpg|jpeg|png|gif|webp)(?:\?|$)",
                url,
                re.IGNORECASE,
            ):
                candidates.append(url)
        elif name == "image":
            if child.text:
                candidates.append(child.text.strip())
            for grandchild in child:
                if local_name(grandchild.tag).lower() == "url" and grandchild.text:
                    candidates.append(grandchild.text.strip())

    candidates.extend(extract_images_from_html(raw_summary))
    return first_image_url(candidates, base_url)


def fetch_article_image(link: str, settings: Settings) -> str:
    request = urllib.request.Request(
        link,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=settings.request_timeout) as response:
        data = response.read(MAX_ARTICLE_BYTES + 1)
        final_url = response.geturl()
        charset = response.headers.get_content_charset() or "utf-8"
    if len(data) > MAX_ARTICLE_BYTES:
        raise ValueError("article response exceeded maximum allowed size")
    document = data.decode(charset, errors="replace")
    return first_image_url(extract_images_from_html(document), final_url)


def contains_cjk(value: str) -> bool:
    return any(
        "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
        for char in value
    )


def translate_text(value: str, target_language: str, settings: Settings) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if not value:
        return ""
    value = value[:MAX_TRANSLATION_CHARS]
    query = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": "auto",
            "tl": target_language,
            "dt": "t",
            "q": value,
        }
    )
    request = urllib.request.Request(
        f"https://translate.googleapis.com/translate_a/single?{query}",
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/json,text/plain",
        },
    )
    with urllib.request.urlopen(request, timeout=settings.request_timeout) as response:
        data = response.read(MAX_TRANSLATION_BYTES + 1)
    if len(data) > MAX_TRANSLATION_BYTES:
        raise ValueError("translation response exceeded maximum allowed size")

    payload = json.loads(data.decode("utf-8", errors="replace"))
    parts = payload[0] if isinstance(payload, list) and payload else []
    translated = "".join(
        part[0]
        for part in parts
        if isinstance(part, list) and part and isinstance(part[0], str)
    )
    return re.sub(r"\s+", " ", translated).strip()


def safe_translate(value: str, target_language: str, settings: Settings) -> str:
    try:
        return translate_text(value, target_language, settings)
    except (OSError, TimeoutError, socket.timeout, ValueError, json.JSONDecodeError) as exc:
        logging.debug("Translation failed for target %s: %s", target_language, exc)
        return ""


def extract_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for output in payload.get("output", []) or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def extract_json_payload(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("OpenAI response JSON must be an object")
    return payload


def openai_news_prompt(settings: Settings) -> str:
    now = datetime.now(settings.timezone)
    return f"""\
请使用实时网页搜索获取新闻，并只返回 JSON，不要返回 Markdown。

任务：
1. 获取截至 {now.strftime('%Y-%m-%d %H:%M')} {settings.timezone_name} 的全球热点新闻 TOP {settings.top_n}。
2. 获取截至 {now.strftime('%Y-%m-%d %H:%M')} {settings.timezone_name} 的科技新闻 TOP {settings.top_n}。
3. 每条新闻必须是真实新闻，避免重复、占位页、聚合页说明、广告和“feed unavailable”内容。
4. 新闻链接必须尽量指向原文报道或可信媒体报道。
5. 摘要需要中英双语，中文摘要 60-120 字，英文摘要 1-2 句。
6. 如能找到新闻图片，请给出可公开访问的 image_url；找不到则留空字符串。

JSON schema:
{{
  "global": [
    {{
      "title_cn": "中文标题",
      "title_en": "English title",
      "summary_cn": "中文摘要",
      "summary_en": "English summary",
      "url": "https://...",
      "image_url": "https://... or empty string",
      "source": "媒体名称",
      "published": "发布时间或 Unknown"
    }}
  ],
  "technology": [
    {{
      "title_cn": "中文标题",
      "title_en": "English title",
      "summary_cn": "中文摘要",
      "summary_en": "English summary",
      "url": "https://...",
      "image_url": "https://... or empty string",
      "source": "媒体名称",
      "published": "发布时间或 Unknown"
    }}
  ]
}}
"""


def call_openai_responses(settings: Settings, web_search_tool: str) -> dict[str, Any]:
    url = f"{settings.openai_base_url}/responses"
    tool_config: dict[str, Any] = {"type": web_search_tool}
    if web_search_tool == "web_search":
        tool_config["search_content_types"] = ["text", "image"]
    body = {
        "model": settings.openai_model,
        "input": openai_news_prompt(settings),
        "tools": [tool_config],
        "tool_choice": "required",
        "max_output_tokens": 6000,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(settings.request_timeout, 60),
        ) as response:
            data = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise parse_openai_http_error(exc.code, detail) from exc
    return json.loads(data.decode("utf-8", errors="replace"))


def parse_openai_http_error(status_code: int, detail: str) -> OpenAINewsError:
    message = detail.strip()
    code = ""
    try:
        payload = json.loads(detail)
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            message = str(error.get("message") or message).strip()
            code = str(error.get("code") or error.get("type") or "").strip()
    except json.JSONDecodeError:
        pass

    if status_code == 429 and code == "insufficient_quota":
        return OpenAINewsError(
            "OpenAI 额度不足或账单不可用，请检查 plan/billing；本次已自动回退 RSS 新闻源。",
            retryable=False,
        )
    if status_code == 401:
        return OpenAINewsError(
            "OpenAI API Key 无效或未授权，请检查页面里的 OpenAI API Key。",
            retryable=False,
        )
    if status_code == 403:
        return OpenAINewsError(
            "OpenAI 账号或模型没有权限执行该请求，请检查模型和 Web Search 权限。",
            retryable=False,
        )

    if len(message) > 260:
        message = message[:257].rstrip() + "..."
    suffix = f" ({code})" if code else ""
    return OpenAINewsError(f"OpenAI API HTTP {status_code}{suffix}: {message}")


def call_openai_news(settings: Settings) -> dict[str, Any]:
    tools_to_try = [settings.openai_web_search_tool]
    if settings.openai_web_search_tool != "web_search_preview":
        tools_to_try.append("web_search_preview")

    errors: list[str] = []
    for tool in tools_to_try:
        try:
            response = call_openai_responses(settings, tool)
            text = extract_response_text(response)
            if not text:
                raise ValueError("OpenAI response did not include output text")
            return extract_json_payload(text)
        except OpenAINewsError as exc:
            errors.append(f"{tool}: {exc}")
            logging.warning("OpenAI news fetch skipped with %s: %s", tool, exc)
            if not exc.retryable:
                break
        except (
            OSError,
            TimeoutError,
            socket.timeout,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            errors.append(f"{tool}: {exc}")
            logging.warning("OpenAI news fetch failed with %s: %s", tool, exc)
    raise OpenAINewsError("; ".join(errors), retryable=False)


def news_item_from_openai(record: dict[str, Any], settings: Settings) -> NewsItem | None:
    if not isinstance(record, dict):
        return None
    title = str(record.get("title_cn") or record.get("title") or "").strip()
    summary = str(record.get("summary_cn") or record.get("summary") or "").strip()
    link = str(record.get("url") or record.get("link") or "").strip()
    if not title or not summary or not link:
        return None
    if is_unavailable_feed_item(title, summary, link):
        return None

    image_url = normalize_asset_url(str(record.get("image_url") or ""), link)
    item = NewsItem(
        title=shorten(title, 180),
        summary=shorten(summary, 260),
        link=link,
        source=str(record.get("source") or source_from_link(link) or "Unknown").strip(),
        published=str(record.get("published") or "Unknown").strip(),
        image_url=image_url,
        title_en=shorten(str(record.get("title_en") or title), 180),
        summary_en=shorten(str(record.get("summary_en") or summary), 260),
    )
    return enrich_news_item(item, settings)


def collect_openai_news(
    settings: Settings,
) -> tuple[list[NewsItem], list[NewsItem], list[FeedError]]:
    try:
        payload = call_openai_news(settings)
    except ValueError as exc:
        return [], [], [FeedError(url="openai://responses", message=str(exc))]

    global_items = [
        item
        for item in (
            news_item_from_openai(record, settings)
            for record in payload.get("global", []) or []
        )
        if item is not None
    ][: settings.top_n]

    tech_items = [
        item
        for item in (
            news_item_from_openai(record, settings)
            for record in payload.get("technology", []) or []
        )
        if item is not None
    ][: settings.top_n]

    errors: list[FeedError] = []
    if not global_items:
        errors.append(FeedError(url="openai://global", message="OpenAI 未返回全球热点新闻。"))
    if not tech_items:
        errors.append(FeedError(url="openai://technology", message="OpenAI 未返回科技新闻。"))
    return global_items, tech_items, errors


def news_candidates_for_model(items: list[NewsItem], limit: int) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for index, item in enumerate(items[:limit], 1):
        candidates.append(
            {
                "id": str(index),
                "title": item.title,
                "summary": item.summary,
                "url": item.link,
                "source": item.source,
                "published": item.published,
                "image_url": item.image_url,
            }
        )
    return candidates


def open_source_news_prompt(
    settings: Settings,
    global_candidates: list[NewsItem],
    tech_candidates: list[NewsItem],
) -> str:
    now = datetime.now(settings.timezone)
    payload = {
        "generated_at": f"{now.strftime('%Y-%m-%d %H:%M')} {settings.timezone_name}",
        "top_n": settings.top_n,
        "global_candidates": news_candidates_for_model(
            global_candidates,
            settings.open_source_candidate_count,
        ),
        "technology_candidates": news_candidates_for_model(
            tech_candidates,
            settings.open_source_candidate_count,
        ),
    }
    return f"""\
你是一个新闻编辑。请只根据下面给出的 RSS 候选新闻进行筛选、排序和摘要，不要编造候选列表以外的新闻。

要求：
1. 从 global_candidates 中选出全球热点新闻 TOP {settings.top_n}。
2. 从 technology_candidates 中选出科技新闻 TOP {settings.top_n}。
3. 去除重复、占位页、广告、摘要不可用内容。
4. 每条新闻保留原始 url 和 image_url；如果候选 image_url 为空，输出空字符串。
5. 输出中英双语标题和摘要。中文摘要 60-120 字；英文摘要 1-2 句。
6. 只输出 JSON，不要 Markdown，不要解释。

JSON schema:
{{
  "global": [
    {{
      "title_cn": "中文标题",
      "title_en": "English title",
      "summary_cn": "中文摘要",
      "summary_en": "English summary",
      "url": "候选新闻 url",
      "image_url": "候选 image_url 或空字符串",
      "source": "媒体名称",
      "published": "发布时间或 Unknown"
    }}
  ],
  "technology": [
    {{
      "title_cn": "中文标题",
      "title_en": "English title",
      "summary_cn": "中文摘要",
      "summary_en": "English summary",
      "url": "候选新闻 url",
      "image_url": "候选 image_url 或空字符串",
      "source": "媒体名称",
      "published": "发布时间或 Unknown"
    }}
  ]
}}

候选新闻 JSON:
{json.dumps(payload, ensure_ascii=False)}
"""


def post_json(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
    max_bytes: int = 2 * 1024 * 1024,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(max_bytes)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        detail = re.sub(r"\s+", " ", detail).strip()
        if len(detail) > 240:
            detail = detail[:237].rstrip() + "..."
        raise OpenSourceNewsError(f"HTTP {exc.code}: {detail}") from exc
    return json.loads(data.decode("utf-8", errors="replace"))


def open_source_auth_headers(settings: Settings) -> dict[str, str]:
    if not settings.open_source_api_key:
        return {}
    return {"Authorization": f"Bearer {settings.open_source_api_key}"}


def call_ollama_news(settings: Settings, prompt: str) -> dict[str, Any]:
    payload = post_json(
        f"{settings.open_source_base_url}/api/chat",
        {
            "model": settings.open_source_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是严谨的新闻编辑，只输出合法 JSON。",
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        },
        open_source_auth_headers(settings),
        timeout=max(settings.request_timeout, 120),
    )
    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    content = ""
    if isinstance(message, dict):
        content = str(message.get("content") or "")
    content = content or str(payload.get("response") or "")
    if not content.strip():
        raise OpenSourceNewsError("Ollama 未返回内容。")
    return extract_json_payload(content)


def call_openai_compatible_news(settings: Settings, prompt: str) -> dict[str, Any]:
    payload = post_json(
        f"{settings.open_source_base_url}/chat/completions",
        {
            "model": settings.open_source_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是严谨的新闻编辑，只输出合法 JSON。",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        open_source_auth_headers(settings),
        timeout=max(settings.request_timeout, 120),
    )
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    if not choices:
        raise OpenSourceNewsError("OpenAI-compatible 服务未返回 choices。")
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = str(message.get("content") or "")
    if not content.strip():
        raise OpenSourceNewsError("OpenAI-compatible 服务未返回内容。")
    return extract_json_payload(content)


def call_open_source_news(
    settings: Settings,
    global_candidates: list[NewsItem],
    tech_candidates: list[NewsItem],
) -> dict[str, Any]:
    if not global_candidates and not tech_candidates:
        raise OpenSourceNewsError("没有可供开源大模型筛选的 RSS 候选新闻。")

    prompt = open_source_news_prompt(settings, global_candidates, tech_candidates)
    if settings.open_source_provider == "ollama":
        return call_ollama_news(settings, prompt)
    if settings.open_source_provider == "openai_compatible":
        return call_openai_compatible_news(settings, prompt)
    raise OpenSourceNewsError("开源大模型 Provider 只能是 ollama 或 openai_compatible。")


def collect_open_source_news(
    settings: Settings,
    global_candidates: list[NewsItem],
    tech_candidates: list[NewsItem],
) -> tuple[list[NewsItem], list[NewsItem], list[FeedError]]:
    try:
        payload = call_open_source_news(settings, global_candidates, tech_candidates)
    except (OpenSourceNewsError, OSError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
        return [], [], [FeedError(url="opensource://news-model", message=str(exc))]

    global_items = [
        item
        for item in (
            news_item_from_openai(record, settings)
            for record in payload.get("global", []) or []
        )
        if item is not None
    ][: settings.top_n]

    tech_records = payload.get("technology", payload.get("tech", [])) or []
    tech_items = [
        item
        for item in (
            news_item_from_openai(record, settings)
            for record in tech_records
        )
        if item is not None
    ][: settings.top_n]

    errors: list[FeedError] = []
    if not global_items:
        errors.append(FeedError(url="opensource://global", message="开源大模型未返回全球热点新闻。"))
    if not tech_items:
        errors.append(FeedError(url="opensource://technology", message="开源大模型未返回科技新闻。"))
    return global_items, tech_items, errors


def enrich_news_item(item: NewsItem, settings: Settings) -> NewsItem:
    title = item.title
    summary = item.summary
    title_en = item.title_en
    summary_en = item.summary_en
    image_url = item.image_url if settings.include_images else ""

    if settings.include_images and settings.fetch_article_images and not image_url:
        try:
            image_url = fetch_article_image(item.link, settings)
        except (OSError, TimeoutError, socket.timeout, ValueError) as exc:
            logging.debug("Article image lookup failed for %s: %s", item.link, exc)

    if settings.bilingual and not (title_en and summary_en):
        source_is_cjk = contains_cjk(f"{title} {summary}")
        if source_is_cjk:
            title_en = title_en or safe_translate(title, "en", settings) or title
            summary_en = summary_en or safe_translate(summary, "en", settings) or summary
        else:
            title_en = title_en or title
            summary_en = summary_en or summary
            title = safe_translate(title, "zh-CN", settings) or title
            summary = safe_translate(summary, "zh-CN", settings) or summary

    return replace(
        item,
        title=title,
        summary=summary,
        title_en=title_en,
        summary_en=summary_en,
        image_url=image_url,
    )


def parse_rss_items(data: bytes, feed_url: str = "") -> list[NewsItem]:
    root = ET.fromstring(data)
    entries = [
        element
        for element in root.iter()
        if local_name(element.tag) in {"item", "entry"}
    ]

    items: list[NewsItem] = []
    for entry in entries:
        title = html_to_text(child_text(entry, ["title"]))
        raw_link = child_text(entry, ["link"]) or child_attr(entry, "link", "href")
        link = normalize_asset_url(raw_link, feed_url) or raw_link
        raw_summary = child_text(entry, ["description", "summary", "content"])
        summary = shorten(html_to_text(raw_summary or title))
        source = html_to_text(child_text(entry, ["source"])) or source_from_link(link)
        published = child_text(entry, ["pubDate", "published", "updated"])
        image_url = extract_rss_image_url(entry, raw_summary, link or feed_url)

        if title and link:
            if is_unavailable_feed_item(title, summary, link):
                logging.warning("Skip unavailable Google News feed placeholder from %s", feed_url)
                continue
            items.append(
                NewsItem(
                    title=shorten(title, 180),
                    summary=summary,
                    link=link,
                    source=source or "Unknown",
                    published=published or "Unknown",
                    image_url=image_url,
                )
            )
    return items


def fetch_feed(url: str, settings: Settings) -> list[NewsItem]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=settings.request_timeout) as response:
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("feed response exceeded maximum allowed size")
    return parse_rss_items(data, url)


def normalize_key(item: NewsItem) -> str:
    title = re.sub(r"\W+", "", item.title.lower())
    return title or item.link


def expanded_feeds(configured_feeds: list[str], fallback_feeds: list[str]) -> list[str]:
    feeds: list[str] = []
    seen: set[str] = set()
    for feed in [*configured_feeds, *fallback_feeds]:
        normalized = feed.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        feeds.append(normalized)
    return feeds


def collect_news(
    feeds: list[str],
    settings: Settings,
) -> tuple[list[NewsItem], list[FeedError]]:
    seen: set[str] = set()
    items: list[NewsItem] = []
    errors: list[FeedError] = []

    for feed in feeds:
        try:
            for item in fetch_feed(feed, settings):
                key = normalize_key(item)
                if key in seen:
                    continue
                seen.add(key)
                items.append(enrich_news_item(item, settings))
                if len(items) >= settings.top_n:
                    break
        except (ET.ParseError, OSError, TimeoutError, socket.timeout, ValueError) as exc:
            errors.append(FeedError(url=feed, message=str(exc)))
        if len(items) >= settings.top_n:
            break

    return items[: settings.top_n], errors


def collect_rss_candidates(
    feeds: list[str],
    settings: Settings,
    limit: int,
) -> tuple[list[NewsItem], list[FeedError]]:
    seen: set[str] = set()
    items: list[NewsItem] = []
    errors: list[FeedError] = []

    for feed in feeds:
        try:
            for item in fetch_feed(feed, settings):
                key = normalize_key(item)
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
                if len(items) >= limit:
                    break
        except (ET.ParseError, OSError, TimeoutError, socket.timeout, ValueError) as exc:
            errors.append(FeedError(url=feed, message=str(exc)))
        if len(items) >= limit:
            break

    return items, errors


def enrich_candidate_items(
    candidates: list[NewsItem],
    settings: Settings,
    limit: int,
) -> list[NewsItem]:
    enriched: list[NewsItem] = []
    seen: set[str] = set()
    for item in candidates:
        key = normalize_key(item)
        if key in seen:
            continue
        seen.add(key)
        enriched.append(enrich_news_item(item, settings))
        if len(enriched) >= limit:
            break
    return enriched


def merge_news_items(primary: list[NewsItem], secondary: list[NewsItem], top_n: int) -> list[NewsItem]:
    merged: list[NewsItem] = []
    seen: set[str] = set()
    for item in [*primary, *secondary]:
        key = normalize_key(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= top_n:
            break
    return merged


def render_plain_section(
    title_cn: str,
    title_en: str,
    items: list[NewsItem],
    settings: Settings,
) -> str:
    lines = [f"{title_cn} / {title_en}"]
    if not items:
        lines.append("未获取到新闻。")
        return "\n".join(lines)

    for index, item in enumerate(items, 1):
        lines.append(f"{index}. {item.title}")
        if settings.bilingual:
            lines.append(f"   English Title: {item.title_en or item.title}")
        lines.append(f"   中文摘要：{item.summary}")
        if settings.bilingual:
            lines.append(f"   English Summary: {item.summary_en or item.summary}")
        lines.extend(
            [
                f"   来源 / Source：{item.source}",
                f"   时间 / Published：{item.published}",
                f"   链接 / Link：{item.link}",
            ]
        )
        if settings.include_images and item.image_url:
            lines.append(f"   图片 / Image：{item.image_url}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_html_item(index: int, item: NewsItem, settings: Settings) -> str:
    image_cell = ""
    if settings.include_images and item.image_url:
        image_cell = f"""
        <td width="184" valign="top" style="padding: 0 18px 0 0;">
          <img src="{html.escape(item.image_url, quote=True)}" alt="{html.escape(item.title, quote=True)}" width="184" style="display:block;width:184px;max-width:184px;height:auto;border-radius:10px;border:1px solid #e5e7eb;">
        </td>
"""

    english_block = ""
    if settings.bilingual:
        english_block = f"""
          <div style="margin-top:12px;padding-top:12px;border-top:1px solid #edf0f5;">
            <p style="margin:0 0 6px;font-size:13px;line-height:1.5;color:#475467;"><strong style="color:#1f2937;">English Title</strong><br>{html.escape(item.title_en or item.title)}</p>
            <p style="margin:0;font-size:13px;line-height:1.6;color:#475467;"><strong style="color:#1f2937;">English Summary</strong><br>{html.escape(item.summary_en or item.summary)}</p>
          </div>
"""

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 16px;background:#ffffff;border:1px solid #e6ebf2;border-radius:14px;border-collapse:separate;">
      <tr>
        <td style="padding:18px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr>
              {image_cell}
              <td valign="top" style="padding:0;">
                <p style="margin:0 0 9px;font-size:12px;letter-spacing:0;color:#0f766e;font-weight:700;">#{index} · {html.escape(item.source)}</p>
                <h3 style="margin:0 0 10px;font-size:18px;line-height:1.35;color:#111827;">{html.escape(item.title)}</h3>
                <p style="margin:0;font-size:14px;line-height:1.7;color:#344054;"><strong>中文摘要</strong><br>{html.escape(item.summary)}</p>
                {english_block}
                <p style="margin:14px 0 0;font-size:12px;line-height:1.6;color:#667085;">时间 / Published：{html.escape(item.published)}</p>
                <p style="margin:10px 0 0;">
                  <a href="{html.escape(item.link, quote=True)}" style="display:inline-block;background:#0f766e;color:#ffffff;text-decoration:none;font-size:13px;font-weight:700;padding:9px 13px;border-radius:7px;">阅读全文 / Read more</a>
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
"""


def render_html_section(
    title_cn: str,
    title_en: str,
    items: list[NewsItem],
    settings: Settings,
) -> str:
    if not items:
        body = """
        <div style="background:#ffffff;border:1px solid #e6ebf2;border-radius:12px;padding:18px;color:#667085;">
          未获取到新闻。 / No news items were collected.
        </div>
"""
    else:
        body = "".join(
            render_html_item(index, item, settings)
            for index, item in enumerate(items, 1)
        )
    return f"""
    <tr>
      <td style="padding:24px 24px 6px;">
        <h2 style="margin:0;font-size:21px;line-height:1.3;color:#111827;">{html.escape(title_cn)}</h2>
        <p style="margin:4px 0 16px;font-size:13px;color:#667085;">{html.escape(title_en)}</p>
        {body}
      </td>
    </tr>
"""


def build_email(
    settings: Settings,
    global_items: list[NewsItem],
    tech_items: list[NewsItem],
    errors: list[FeedError],
) -> EmailMessage:
    now = datetime.now(settings.timezone)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    date_label = now.strftime("%Y-%m-%d")

    subject = (
        f"每日新闻简报 / Daily News Brief | {date_label} | "
        f"全球热点TOP{settings.top_n} + Tech TOP{settings.top_n}"
    )
    plain_parts = [
        "每日新闻简报 / Daily News Brief",
        f"生成时间 / Generated at：{stamp} {settings.timezone_name}",
        "",
        render_plain_section(
            f"一、全球热点 TOP {settings.top_n}",
            f"Global Hotspots TOP {settings.top_n}",
            global_items,
            settings,
        ),
        "",
        render_plain_section(
            f"二、科技新闻 TOP {settings.top_n}",
            f"Technology News TOP {settings.top_n}",
            tech_items,
            settings,
        ),
    ]

    if errors:
        plain_parts.extend(
            [
                "",
                "抓取提示：",
                *[f"- {error.url}: {error.message}" for error in errors],
            ]
        )

    html_errors = ""
    if errors:
        html_errors = """
    <tr>
      <td style="padding:8px 24px 24px;">
        <div style="background:#fffaeb;border:1px solid #fedf89;border-radius:12px;padding:16px;">
          <h2 style="margin:0 0 10px;font-size:16px;color:#93370d;">抓取提示 / Fetch Notes</h2>
          <ul style="margin:0;padding-left:18px;color:#7a2e0e;font-size:13px;line-height:1.6;">
""" + "".join(
            f"<li>{html.escape(error.url)}: {html.escape(error.message)}</li>"
            for error in errors
        ) + """
          </ul>
        </div>
      </td>
    </tr>
"""

    html_body = f"""\
<!doctype html>
<html lang="zh-CN">
  <body style="margin:0;padding:0;background:#eef2f7;font-family:Arial,'Microsoft YaHei',sans-serif;color:#1f2937;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f7;margin:0;padding:24px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="680" cellpadding="0" cellspacing="0" style="width:680px;max-width:680px;background:#f8fafc;border-radius:18px;overflow:hidden;border:1px solid #d9e2ec;">
            <tr>
              <td style="background:#15202b;padding:28px 30px;color:#ffffff;">
                <p style="margin:0 0 8px;font-size:13px;color:#b7d8d5;font-weight:700;">{html.escape(stamp)} · {html.escape(settings.timezone_name)}</p>
                <h1 style="margin:0;font-size:28px;line-height:1.25;color:#ffffff;">每日新闻简报</h1>
                <p style="margin:6px 0 0;font-size:15px;color:#d7e5ea;">Daily News Brief with bilingual summaries and images</p>
              </td>
            </tr>
            {render_html_section(f"一、全球热点 TOP {settings.top_n}", f"Global Hotspots TOP {settings.top_n}", global_items, settings)}
            {render_html_section(f"二、科技新闻 TOP {settings.top_n}", f"Technology News TOP {settings.top_n}", tech_items, settings)}
            <tr>
              <td style="padding:6px 24px 24px;">
                <p style="margin:0;font-size:12px;line-height:1.6;color:#667085;">图片来自新闻源或原文页面。部分邮箱客户端可能默认拦截远程图片，点击显示图片即可查看。<br>Images are sourced from RSS feeds or article pages. Some email clients may block remote images by default.</p>
              </td>
            </tr>
            {html_errors}
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from or "news-agent@localhost"
    message["To"] = ", ".join(settings.email_to)
    message["Date"] = formatdate(localtime=True)
    message.set_content("\n".join(plain_parts))
    message.add_alternative(html_body, subtype="html")
    return message


def send_email(message: EmailMessage, settings: Settings) -> None:
    if settings.dry_run:
        logging.info("DRY_RUN enabled; email content follows.")
        plain = message.get_body(preferencelist=("plain",))
        print(f"Subject: {message['Subject']}")
        print(f"From: {message['From']}")
        print(f"To: {message['To']}")
        print("")
        print(plain.get_content() if plain else message.as_string())
        return

    if settings.smtp_ssl:
        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.request_timeout,
        ) as smtp:
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(
        settings.smtp_host,
        settings.smtp_port,
        timeout=settings.request_timeout,
    ) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


def run_job(settings: Settings) -> None:
    global_feeds = expanded_feeds(settings.global_feeds, DEFAULT_GLOBAL_FEEDS)
    tech_feeds = expanded_feeds(settings.tech_feeds, DEFAULT_TECH_FEEDS)
    errors: list[FeedError] = []

    global_items: list[NewsItem] = []
    tech_items: list[NewsItem] = []

    if settings.use_openai_news:
        if settings.openai_api_key:
            logging.info("Collecting news with OpenAI model %s", settings.openai_model)
            global_items, tech_items, openai_errors = collect_openai_news(settings)
            errors.extend(openai_errors)
        else:
            errors.append(
                FeedError(
                    url="openai://responses",
                    message="未配置 OpenAI API Key，已跳过 OpenAI 并尝试开源大模型/RSS。",
                )
            )

    needs_fallback = (
        len(global_items) < settings.top_n
        or len(tech_items) < settings.top_n
    )
    global_candidates: list[NewsItem] = []
    tech_candidates: list[NewsItem] = []

    if needs_fallback:
        candidate_limit = max(settings.top_n, settings.open_source_candidate_count)
        logging.info("Collecting global RSS candidates from %d feed(s)", len(global_feeds))
        global_candidates, global_candidate_errors = collect_rss_candidates(
            global_feeds,
            settings,
            candidate_limit,
        )
        errors.extend(global_candidate_errors)

        logging.info("Collecting technology RSS candidates from %d feed(s)", len(tech_feeds))
        tech_candidates, tech_candidate_errors = collect_rss_candidates(
            tech_feeds,
            settings,
            candidate_limit,
        )
        errors.extend(tech_candidate_errors)

    if needs_fallback and settings.use_open_source_news:
        logging.info(
            "Collecting news with open-source model %s via %s",
            settings.open_source_model,
            settings.open_source_provider,
        )
        open_global_items, open_tech_items, open_source_errors = collect_open_source_news(
            settings,
            global_candidates,
            tech_candidates,
        )
        global_items = merge_news_items(global_items, open_global_items, settings.top_n)
        tech_items = merge_news_items(tech_items, open_tech_items, settings.top_n)
        errors.extend(open_source_errors)

    if len(global_items) < settings.top_n:
        rss_global_items = enrich_candidate_items(
            global_candidates,
            settings,
            settings.top_n,
        )
        global_items = merge_news_items(global_items, rss_global_items, settings.top_n)

    if len(tech_items) < settings.top_n:
        rss_tech_items = enrich_candidate_items(
            tech_candidates,
            settings,
            settings.top_n,
        )
        tech_items = merge_news_items(tech_items, rss_tech_items, settings.top_n)

    message = build_email(
        settings,
        global_items=global_items,
        tech_items=tech_items,
        errors=errors,
    )
    send_email(message, settings)
    logging.info(
        "News email completed: %d global item(s), %d tech item(s), %d error(s)",
        len(global_items),
        len(tech_items),
        len(errors),
    )


def seconds_until_next_run(settings: Settings) -> tuple[float, datetime]:
    now = datetime.now(settings.timezone)
    interval = timedelta(minutes=settings.schedule_interval_minutes)
    next_run = now.replace(
        hour=settings.schedule_hour,
        minute=settings.schedule_minute,
        second=0,
        microsecond=0,
    )
    while next_run <= now:
        next_run += interval
    return (next_run - now).total_seconds(), next_run


class AgentState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.next_run: datetime | None = None
        self.running = False
        self.current_source = ""
        self.last_started_at: datetime | None = None
        self.last_finished_at: datetime | None = None
        self.last_status = "尚未运行"
        self.last_error = ""

    def set_next_run(self, next_run: datetime | None) -> None:
        with self.lock:
            self.next_run = next_run

    def claim_run(self, source: str) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.current_source = source
            self.last_started_at = datetime.now().astimezone()
            self.last_status = "运行中"
            self.last_error = ""
            return True

    def finish_run(self, success: bool, error: str = "") -> None:
        with self.lock:
            self.running = False
            self.last_finished_at = datetime.now().astimezone()
            self.last_status = "成功" if success else "失败"
            self.last_error = error
            self.current_source = ""

    def record_skip(self, status: str, error: str) -> None:
        with self.lock:
            self.running = False
            self.current_source = ""
            self.last_finished_at = datetime.now().astimezone()
            self.last_status = status
            self.last_error = error

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "next_run": self.next_run,
                "running": self.running,
                "current_source": self.current_source,
                "last_started_at": self.last_started_at,
                "last_finished_at": self.last_finished_at,
                "last_status": self.last_status,
                "last_error": self.last_error,
            }


class AgentApp:
    def __init__(self, config_store: ConfigStore) -> None:
        self.config_store = config_store
        self.state = AgentState()
        self.wake_event = threading.Event()
        self.sessions: dict[str, float] = {}
        self.sessions_lock = threading.RLock()

    def wake_scheduler(self) -> None:
        self.wake_event.set()

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self.sessions_lock:
            self.sessions[token] = time.time() + 24 * 60 * 60
        return token

    def destroy_session(self, token: str) -> None:
        with self.sessions_lock:
            self.sessions.pop(token, None)

    def is_valid_session(self, token: str) -> bool:
        now = time.time()
        with self.sessions_lock:
            expires_at = self.sessions.get(token)
            if not expires_at:
                return False
            if expires_at < now:
                self.sessions.pop(token, None)
                return False
            self.sessions[token] = now + 24 * 60 * 60
            return True

    def start_job_thread(self, source: str) -> str:
        settings = self.config_store.load_settings()
        try:
            validate_settings(settings)
        except ValueError as exc:
            logging.warning("Skip %s job because configuration is incomplete: %s", source, exc)
            self.state.record_skip("配置不完整", str(exc))
            return "invalid"

        if not self.state.claim_run(source):
            return "busy"
        thread = threading.Thread(
            target=self._run_claimed_job,
            args=(settings,),
            daemon=True,
        )
        thread.start()
        return "started"

    def run_job_sync(self, settings: Settings, source: str) -> bool:
        try:
            validate_settings(settings)
        except ValueError as exc:
            logging.warning("Skip %s job because configuration is incomplete: %s", source, exc)
            self.state.record_skip("配置不完整", str(exc))
            return False

        if not self.state.claim_run(source):
            logging.warning("Skip %s job because another job is running", source)
            return False
        self._run_claimed_job(settings)
        return True

    def _run_claimed_job(self, settings: Settings) -> None:
        try:
            validate_settings(settings)
            run_job(settings)
        except ValueError as exc:
            logging.warning("News job skipped: %s", exc)
            self.state.finish_run(False, str(exc))
        except Exception as exc:
            logging.exception("News job failed")
            self.state.finish_run(False, str(exc))
        else:
            self.state.finish_run(True)

    def run_scheduler(self) -> None:
        logging.info("Scheduler started")
        while True:
            try:
                settings = self.config_store.load_settings()
                if not settings.enabled:
                    self.state.set_next_run(None)
                    logging.info("Scheduler is disabled")
                    self.wake_event.wait(3600)
                    self.wake_event.clear()
                    continue

                delay, next_run = seconds_until_next_run(settings)
                self.state.set_next_run(next_run)
                logging.info(
                    "Next run at %s, interval %d minute(s)",
                    next_run.isoformat(timespec="seconds"),
                    settings.schedule_interval_minutes,
                )
                if self.wake_event.wait(delay):
                    self.wake_event.clear()
                    continue

                self.run_job_sync(settings, "scheduled")
            except Exception:
                logging.exception("Scheduler loop failed")
                self.wake_event.wait(60)
                self.wake_event.clear()


def form_value(fields: dict[str, list[str]], name: str, default: str = "") -> str:
    values = fields.get(name)
    if not values:
        return default
    return values[0].strip()


def form_bool(fields: dict[str, list[str]], name: str) -> bool:
    return name in fields


def config_from_form(
    current: dict[str, Any],
    fields: dict[str, list[str]],
) -> dict[str, Any]:
    config = dict(current)
    admin_username = form_value(fields, "admin_username")
    if not admin_username:
        raise ValueError("管理员账号不能为空。")
    config["admin_username"] = admin_username

    new_password = form_value(fields, "admin_password")
    confirm_password = form_value(fields, "admin_password_confirm")
    if new_password:
        if len(new_password) < 8:
            raise ValueError("管理员新密码至少需要 8 位。")
        if new_password != confirm_password:
            raise ValueError("两次输入的管理员新密码不一致。")
        config["admin_password_hash"] = hash_password(new_password)
        config["admin_password_changed"] = True

    config["use_openai_news"] = form_bool(fields, "use_openai_news")
    openai_api_key = form_value(fields, "openai_api_key")
    if openai_api_key:
        config["openai_api_key"] = openai_api_key
    if form_bool(fields, "clear_openai_api_key"):
        config["openai_api_key"] = ""
    config["openai_model"] = form_value(fields, "openai_model", "gpt-4.1")
    config["openai_base_url"] = form_value(
        fields,
        "openai_base_url",
        "https://api.openai.com/v1",
    ).rstrip("/")
    config["openai_web_search_tool"] = form_value(
        fields,
        "openai_web_search_tool",
        "web_search",
    )
    config["use_open_source_news"] = form_bool(fields, "use_open_source_news")
    config["open_source_provider"] = form_value(fields, "open_source_provider", "ollama")
    if config["open_source_provider"] not in {"ollama", "openai_compatible"}:
        raise ValueError("开源大模型 Provider 只能是 ollama 或 openai_compatible。")
    config["open_source_base_url"] = form_value(
        fields,
        "open_source_base_url",
        "http://host.docker.internal:11434",
    ).rstrip("/")
    config["open_source_model"] = form_value(
        fields,
        "open_source_model",
        "qwen2.5:7b",
    )
    open_source_api_key = form_value(fields, "open_source_api_key")
    if open_source_api_key:
        config["open_source_api_key"] = open_source_api_key
    if form_bool(fields, "clear_open_source_api_key"):
        config["open_source_api_key"] = ""
    config["open_source_candidate_count"] = int(
        form_value(fields, "open_source_candidate_count", "30")
    )
    if config["open_source_candidate_count"] < 10 or config["open_source_candidate_count"] > 100:
        raise ValueError("开源模型候选新闻数必须在 10 到 100 之间。")

    timezone_name = form_value(fields, "timezone_name", "Asia/Shanghai")
    ZoneInfo(timezone_name)
    config["timezone_name"] = timezone_name

    schedule_start_time = form_value(fields, "schedule_start_time", "08:30")
    parse_schedule_time(schedule_start_time)
    config["schedule_start_time"] = schedule_start_time

    schedule_interval_minutes = int(
        form_value(fields, "schedule_interval_minutes", "1440")
    )
    if schedule_interval_minutes < 1:
        raise ValueError("相隔时间必须大于等于 1 分钟。")
    config["schedule_interval_minutes"] = schedule_interval_minutes

    config["enabled"] = form_bool(fields, "enabled")
    config["email_to"] = parse_list(form_value(fields, "email_to"), [])
    if not config["email_to"]:
        raise ValueError("收件邮箱不能为空。")

    config["smtp_host"] = form_value(fields, "smtp_host")
    config["smtp_port"] = int(form_value(fields, "smtp_port", "465"))
    if config["smtp_port"] < 1:
        raise ValueError("SMTP 端口必须大于等于 1。")
    config["smtp_username"] = form_value(fields, "smtp_username")
    config["smtp_from"] = form_value(fields, "smtp_from") or config["smtp_username"]
    smtp_password = form_value(fields, "smtp_password")
    if smtp_password:
        config["smtp_password"] = smtp_password
    if form_bool(fields, "clear_smtp_password"):
        config["smtp_password"] = ""
    config["smtp_ssl"] = form_bool(fields, "smtp_ssl")
    config["smtp_starttls"] = form_bool(fields, "smtp_starttls")
    config["bilingual"] = form_bool(fields, "bilingual")
    config["include_images"] = form_bool(fields, "include_images")
    config["fetch_article_images"] = form_bool(fields, "fetch_article_images")

    config["top_n"] = int(form_value(fields, "top_n", "10"))
    if config["top_n"] < 1 or config["top_n"] > 50:
        raise ValueError("TOP 条数必须在 1 到 50 之间。")
    config["request_timeout"] = int(form_value(fields, "request_timeout", "20"))
    if config["request_timeout"] < 1:
        raise ValueError("请求超时必须大于等于 1 秒。")
    config["user_agent"] = form_value(fields, "user_agent", DEFAULT_USER_AGENT)
    config["dry_run"] = form_bool(fields, "dry_run")

    config["global_feeds"] = parse_list(form_value(fields, "global_feeds"), [])
    config["tech_feeds"] = parse_list(form_value(fields, "tech_feeds"), [])
    if not config["global_feeds"]:
        raise ValueError("全球热点 RSS 新闻源不能为空。")
    if not config["tech_feeds"]:
        raise ValueError("科技新闻 RSS 新闻源不能为空。")

    settings_from_config(config)
    return config


def format_dt(value: datetime | None) -> str:
    if not value:
        return "未设置"
    return value.isoformat(timespec="seconds")


def textarea_value(values: list[str]) -> str:
    return "\n".join(values)


def checked(value: bool) -> str:
    return " checked" if value else ""


def render_status(app: AgentApp, config: dict[str, Any]) -> str:
    state = app.state.snapshot()
    try:
        validate_settings(settings_from_config(config))
        config_status = "配置完整"
        config_class = "ok"
    except Exception as exc:
        config_status = str(exc)
        config_class = "warn"

    next_run = state["next_run"]
    rows = [
        ("任务状态", "启用" if parse_bool_value(config.get("enabled"), True) else "已停用"),
        ("配置检查", config_status),
        ("下一次运行", format_dt(next_run)),
        ("当前运行", state["current_source"] if state["running"] else "无"),
        ("上次开始", format_dt(state["last_started_at"])),
        ("上次完成", format_dt(state["last_finished_at"])),
        ("上次结果", state["last_status"]),
    ]
    if state["last_error"]:
        rows.append(("错误信息", state["last_error"]))

    items = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"
        for label, value in rows
    )
    password_warning = ""
    if not parse_bool_value(config.get("admin_password_changed"), False):
        password_warning = (
            '<p class="banner warn">当前仍在使用首次启动默认管理员密码，请尽快修改。</p>'
        )
    return f"""
<section class="status-grid {config_class}">
  {items}
</section>
{password_warning}
"""


def render_config_form(config: dict[str, Any], message: str = "", error: str = "") -> str:
    smtp_password_state = "已配置" if config.get("smtp_password") else "未配置"
    openai_key_state = "已配置" if config.get("openai_api_key") else "未配置"
    open_source_key_state = "已配置" if config.get("open_source_api_key") else "未配置"
    open_source_provider = str(config.get("open_source_provider", "ollama"))
    message_html = f'<p class="banner ok">{html.escape(message)}</p>' if message else ""
    error_html = f'<p class="banner error">{html.escape(error)}</p>' if error else ""
    return f"""
{message_html}
{error_html}
<form method="post" action="/save" class="config-form">
  <section>
    <h2>登录设置</h2>
    <label>管理员账号
      <input name="admin_username" value="{html.escape(str(config.get("admin_username", "")), quote=True)}" required>
    </label>
    <div class="grid two">
      <label>新管理员密码
        <input name="admin_password" type="password" autocomplete="new-password" placeholder="留空则不修改">
      </label>
      <label>确认新密码
        <input name="admin_password_confirm" type="password" autocomplete="new-password">
      </label>
    </div>
  </section>

  <section>
    <h2>新闻获取方式</h2>
    <label class="checkbox">
      <input name="use_openai_news" type="checkbox"{checked(parse_bool_value(config.get("use_openai_news"), True))}>
      使用 OpenAI 大模型获取全球热点和科技新闻 TOP10
    </label>
    <div class="grid two">
      <label>OpenAI API Key（{openai_key_state}）
        <input name="openai_api_key" type="password" autocomplete="new-password" placeholder="留空则保持不变">
      </label>
      <label class="checkbox compact">
        <input name="clear_openai_api_key" type="checkbox">
        清空已保存的 OpenAI API Key
      </label>
      <label>OpenAI 模型
        <input name="openai_model" value="{html.escape(str(config.get("openai_model", "gpt-4.1")), quote=True)}" placeholder="gpt-4.1">
      </label>
      <label>OpenAI Base URL
        <input name="openai_base_url" value="{html.escape(str(config.get("openai_base_url", "https://api.openai.com/v1")), quote=True)}">
      </label>
      <label>Web Search 工具类型
        <input name="openai_web_search_tool" value="{html.escape(str(config.get("openai_web_search_tool", "web_search")), quote=True)}" placeholder="web_search">
      </label>
    </div>
    <h2>开源大模型兜底</h2>
    <label class="checkbox">
      <input name="use_open_source_news" type="checkbox"{checked(parse_bool_value(config.get("use_open_source_news"), True))}>
      OpenAI 不可用时使用开源大模型处理 RSS 候选新闻
    </label>
    <div class="grid two">
      <label>Provider
        <select name="open_source_provider">
          <option value="ollama"{' selected' if open_source_provider == 'ollama' else ''}>Ollama</option>
          <option value="openai_compatible"{' selected' if open_source_provider == 'openai_compatible' else ''}>OpenAI-compatible</option>
        </select>
      </label>
      <label>开源模型
        <input name="open_source_model" value="{html.escape(str(config.get("open_source_model", "qwen2.5:7b")), quote=True)}" placeholder="qwen2.5:7b">
      </label>
      <label>开源模型 Base URL
        <input name="open_source_base_url" value="{html.escape(str(config.get("open_source_base_url", "http://host.docker.internal:11434")), quote=True)}">
      </label>
      <label>候选新闻数
        <input name="open_source_candidate_count" type="number" min="10" max="100" value="{html.escape(str(config.get("open_source_candidate_count", 30)), quote=True)}">
      </label>
      <label>开源模型 API Key（{open_source_key_state}）
        <input name="open_source_api_key" type="password" autocomplete="new-password" placeholder="本地 Ollama 通常留空">
      </label>
      <label class="checkbox compact">
        <input name="clear_open_source_api_key" type="checkbox">
        清空已保存的开源模型 API Key
      </label>
    </div>
  </section>

  <section>
    <h2>定时任务</h2>
    <label class="checkbox">
      <input name="enabled" type="checkbox"{checked(parse_bool_value(config.get("enabled"), True))}>
      启用定时发送
    </label>
    <div class="grid three">
      <label>时区
        <input name="timezone_name" value="{html.escape(str(config.get("timezone_name", "Asia/Shanghai")), quote=True)}" required>
      </label>
      <label>起始时间
        <input name="schedule_start_time" type="time" value="{html.escape(str(config.get("schedule_start_time", "08:30")), quote=True)}" required>
      </label>
      <label>相隔时间（分钟）
        <input name="schedule_interval_minutes" type="number" min="1" value="{html.escape(str(config.get("schedule_interval_minutes", 1440)), quote=True)}" required>
      </label>
    </div>
  </section>

  <section>
    <h2>邮件配置</h2>
    <label>收件邮箱（多个邮箱用逗号或换行分隔）
      <textarea name="email_to" required>{html.escape(textarea_value(list(config.get("email_to") or [])))}</textarea>
    </label>
    <div class="grid two">
      <label>SMTP 服务器
        <input name="smtp_host" value="{html.escape(str(config.get("smtp_host", "")), quote=True)}" placeholder="smtp.126.com">
      </label>
      <label>SMTP 端口
        <input name="smtp_port" type="number" min="1" value="{html.escape(str(config.get("smtp_port", 465)), quote=True)}">
      </label>
      <label>SMTP 账号
        <input name="smtp_username" value="{html.escape(str(config.get("smtp_username", "")), quote=True)}" autocomplete="username">
      </label>
      <label>发件邮箱
        <input name="smtp_from" value="{html.escape(str(config.get("smtp_from", "")), quote=True)}">
      </label>
      <label>SMTP 密码/授权码（{smtp_password_state}）
        <input name="smtp_password" type="password" autocomplete="new-password" placeholder="留空则保持不变">
      </label>
      <label class="checkbox compact">
        <input name="clear_smtp_password" type="checkbox">
        清空已保存的 SMTP 密码
      </label>
    </div>
    <div class="checks">
      <label class="checkbox">
        <input name="smtp_ssl" type="checkbox"{checked(parse_bool_value(config.get("smtp_ssl"), True))}>
        使用 SMTP SSL
      </label>
      <label class="checkbox">
        <input name="smtp_starttls" type="checkbox"{checked(parse_bool_value(config.get("smtp_starttls"), False))}>
        使用 STARTTLS
      </label>
      <label class="checkbox">
        <input name="dry_run" type="checkbox"{checked(parse_bool_value(config.get("dry_run"), False))}>
        只生成内容不发送邮件
      </label>
    </div>
  </section>

  <section>
    <h2>邮件内容</h2>
    <div class="checks">
      <label class="checkbox">
        <input name="bilingual" type="checkbox"{checked(parse_bool_value(config.get("bilingual"), True))}>
        中英双语摘要
      </label>
      <label class="checkbox">
        <input name="include_images" type="checkbox"{checked(parse_bool_value(config.get("include_images"), True))}>
        邮件中显示新闻图片
      </label>
      <label class="checkbox">
        <input name="fetch_article_images" type="checkbox"{checked(parse_bool_value(config.get("fetch_article_images"), True))}>
        RSS 无图时从原文页面补图
      </label>
    </div>
  </section>

  <section>
    <h2>新闻源</h2>
    <div class="grid two">
      <label>全球热点 RSS
        <textarea name="global_feeds" required>{html.escape(textarea_value(list(config.get("global_feeds") or [])))}</textarea>
      </label>
      <label>科技新闻 RSS
        <textarea name="tech_feeds" required>{html.escape(textarea_value(list(config.get("tech_feeds") or [])))}</textarea>
      </label>
      <label>每类新闻条数
        <input name="top_n" type="number" min="1" max="50" value="{html.escape(str(config.get("top_n", 10)), quote=True)}">
      </label>
      <label>请求超时（秒）
        <input name="request_timeout" type="number" min="1" value="{html.escape(str(config.get("request_timeout", 20)), quote=True)}">
      </label>
    </div>
    <label>User-Agent
      <input name="user_agent" value="{html.escape(str(config.get("user_agent", DEFAULT_USER_AGENT)), quote=True)}">
    </label>
  </section>

  <div class="actions">
    <button type="submit">保存配置</button>
  </div>
</form>

<form method="post" action="/run-now" class="inline-action">
  <button type="submit" class="secondary">立即运行一次</button>
</form>
"""


PAGE_CSS = """
:root {
  color-scheme: light;
  --bg: #f6f8fb;
  --panel: #ffffff;
  --border: #d9e0ea;
  --text: #1f2937;
  --muted: #65758b;
  --accent: #0f766e;
  --accent-dark: #115e59;
  --error: #b42318;
  --warn: #9a6700;
  --ok: #067647;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Arial, "Microsoft YaHei", sans-serif;
  font-size: 15px;
}
header {
  background: #15202b;
  color: #fff;
  padding: 18px 28px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
header h1 { margin: 0; font-size: 20px; font-weight: 700; }
header a { color: #d8eef0; text-decoration: none; }
main {
  max-width: 1180px;
  margin: 0 auto;
  padding: 24px;
}
section {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 18px;
  margin-bottom: 16px;
}
h2 {
  font-size: 16px;
  margin: 0 0 14px;
}
label {
  display: grid;
  gap: 7px;
  color: var(--muted);
  font-weight: 600;
}
input, textarea, select {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 11px;
  color: var(--text);
  font: inherit;
  background: #fff;
}
textarea {
  min-height: 108px;
  resize: vertical;
}
.grid {
  display: grid;
  gap: 14px;
  margin-bottom: 14px;
}
.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.checkbox {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text);
}
.checkbox input { width: 18px; height: 18px; }
.compact { align-self: end; min-height: 42px; }
.checks {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
}
.actions {
  display: flex;
  gap: 12px;
  margin: 18px 0 8px;
}
button {
  border: 0;
  border-radius: 6px;
  background: var(--accent);
  color: #fff;
  padding: 10px 16px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}
button:hover { background: var(--accent-dark); }
button.secondary {
  background: #344054;
}
.inline-action {
  margin: 0 0 24px;
}
.status-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.status-grid div {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 11px;
  background: #fbfcfe;
}
.status-grid span {
  display: block;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 4px;
}
.status-grid strong {
  display: block;
  overflow-wrap: anywhere;
}
.banner {
  border-radius: 6px;
  padding: 11px 13px;
  margin: 0 0 16px;
  font-weight: 700;
}
.banner.ok { background: #ecfdf3; color: var(--ok); }
.banner.warn { background: #fffaeb; color: var(--warn); }
.banner.error { background: #fef3f2; color: var(--error); }
.login {
  max-width: 420px;
  margin: 70px auto;
}
@media (max-width: 860px) {
  .two, .three, .status-grid { grid-template-columns: 1fr; }
  main { padding: 16px; }
  header { padding: 16px; }
}
"""


def page(title: str, body: str, authenticated: bool = True) -> bytes:
    nav = '<a href="/logout">退出登录</a>' if authenticated else ""
    document = f"""\
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{PAGE_CSS}</style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    {nav}
  </header>
  <main>{body}</main>
</body>
</html>
"""
    return document.encode("utf-8")


def login_page(error: str = "") -> bytes:
    error_html = f'<p class="banner error">{html.escape(error)}</p>' if error else ""
    body = f"""
<section class="login">
  {error_html}
  <form method="post" action="/login">
    <label>管理员账号
      <input name="username" autocomplete="username" required autofocus>
    </label>
    <br>
    <label>管理员密码
      <input name="password" type="password" autocomplete="current-password" required>
    </label>
    <div class="actions">
      <button type="submit">登录</button>
    </div>
  </form>
</section>
"""
    return page("新闻 Agent 登录", body, authenticated=False)


class AgentHTTPServer(ThreadingHTTPServer):
    app: AgentApp


class AgentHandler(BaseHTTPRequestHandler):
    server: AgentHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("HTTP %s - %s", self.address_string(), format % args)

    @property
    def app(self) -> AgentApp:
        return self.server.app

    def do_GET(self) -> None:
        route = urllib.parse.urlparse(self.path)
        if route.path == "/login":
            self.send_html(login_page(), HTTPStatus.OK)
            return
        if route.path == "/logout":
            self.logout()
            self.redirect("/login")
            return
        if not self.require_auth():
            return
        if route.path == "/":
            query = urllib.parse.parse_qs(route.query)
            message = ""
            if "saved" in query:
                message = "配置已保存，定时器已重新计算。"
            elif "started" in query:
                message = "已启动一次手动运行。"
            elif "busy" in query:
                message = "已有任务正在运行，本次手动运行未启动。"
            elif "invalid" in query:
                message = "配置不完整，未启动运行。请先补齐页面中的邮件配置。"
            self.render_dashboard(message=message)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        route = urllib.parse.urlparse(self.path)
        if route.path == "/login":
            self.handle_login()
            return
        if not self.require_auth():
            return
        if route.path == "/save":
            self.handle_save()
            return
        if route.path == "/run-now":
            result = self.app.start_job_thread("manual")
            if result == "started":
                self.redirect("/?started=1")
            elif result == "invalid":
                self.redirect("/?invalid=1")
            else:
                self.redirect("/?busy=1")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_FORM_BYTES:
            raise ValueError("提交内容过大。")
        raw = self.rfile.read(length).decode("utf-8")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def render_dashboard(self, message: str = "", error: str = "") -> None:
        config = self.app.config_store.load()
        body = render_status(self.app, config) + render_config_form(
            config,
            message=message,
            error=error,
        )
        self.send_html(page("新闻 Agent 配置", body), HTTPStatus.OK)

    def handle_login(self) -> None:
        try:
            fields = self.read_form()
        except ValueError as exc:
            self.send_html(login_page(str(exc)), HTTPStatus.BAD_REQUEST)
            return

        username = form_value(fields, "username")
        password = form_value(fields, "password")
        config = self.app.config_store.load()
        expected_username = str(config.get("admin_username", DEFAULT_ADMIN_USERNAME))
        expected_password_hash = str(config.get("admin_password_hash", ""))

        if username != expected_username or not verify_password(
            password,
            expected_password_hash,
        ):
            self.send_html(login_page("账号或密码不正确。"), HTTPStatus.UNAUTHORIZED)
            return

        token = self.app.create_session()
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax",
        )
        self.end_headers()

    def handle_save(self) -> None:
        try:
            fields = self.read_form()
            current = self.app.config_store.load()
            config = config_from_form(current, fields)
            self.app.config_store.save(config)
            self.app.wake_scheduler()
        except Exception as exc:
            logging.exception("Failed to save config")
            self.render_dashboard(error=str(exc))
            return
        self.redirect("/?saved=1")

    def require_auth(self) -> bool:
        token = self.session_token()
        if token and self.app.is_valid_session(token):
            return True
        self.redirect("/login")
        return False

    def session_token(self) -> str:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return ""
        cookie = http.cookies.SimpleCookie(cookie_header)
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def logout(self) -> None:
        token = self.session_token()
        if token:
            self.app.destroy_session(token)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def send_html(self, body: bytes, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_web_server(app: AgentApp) -> None:
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = parse_env_int("WEB_PORT", 8080, minimum=1)
    server = AgentHTTPServer((host, port), AgentHandler)
    server.app = app
    logging.info("Web console listening on http://%s:%d", host, port)
    server.serve_forever()


def default_config_path() -> str:
    return os.getenv("CONFIG_PATH", str(Path("data") / "config.json"))


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> None:
    configure_logging()
    config_store = ConfigStore(default_config_path())
    app = AgentApp(config_store)

    if parse_env_bool("RUN_ONCE", False):
        settings = config_store.load_settings()
        validate_settings(settings)
        run_job(settings)
        return

    scheduler_thread = threading.Thread(
        target=app.run_scheduler,
        name="news-agent-scheduler",
        daemon=True,
    )
    scheduler_thread.start()
    run_web_server(app)


if __name__ == "__main__":
    main()
