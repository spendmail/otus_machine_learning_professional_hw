"""
Парсер краеведческого форума nsk-kraeved.ru (движок PunBB).

Собирает по каждой теме форума:
    - forum_id, forum_name       - подфорум и его название
    - topic_id, title            - тема и её заголовок
    - text                       - текст всех сообщений на первой странице темы
    - views, replies             - число просмотров и ответов (из списка тем)
    - created_ts                 - unix-время создания темы (первого сообщения)
    - scraped_ts                 - unix-время скачивания (для расчёта "возраста" темы)

robots.txt сайта явно разрешает viewforum.php?id= и viewtopic.php?id=.
Между запросами выдерживается пауза (RATE_LIMIT_SEC), чтобы не нагружать сервер.
"""

import csv
import os
import re
import time

import requests
from bs4 import BeautifulSoup

csv.field_size_limit(10_000_000)

BASE_URL = "https://nsk-kraeved.ru"
MAX_TEXT_CHARS = 20_000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (educational HSE ML homework scraper; contact: sizikov@pho.to)"
}
RATE_LIMIT_SEC = 0.4
MAX_RETRIES = 3

# id-заглушки, которые встречаются в sitemap.xml, но не являются настоящими
# подфорумами (отдают страницу "Ссылка... неверная или устаревшая").
KNOWN_INVALID_FORUM_IDS = {0, 999}
INVALID_FORUM_MARKER = "неверная или устаревшая"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TOPICS_META_CSV = os.path.join(DATA_DIR, "topics_meta.csv")
TOPICS_FULL_CSV = os.path.join(DATA_DIR, "nsk_kraeved_topics.csv")

META_FIELDS = ["forum_id", "forum_name", "topic_id", "title", "views", "replies"]
FULL_FIELDS = META_FIELDS + ["text", "created_ts", "scraped_ts"]


def fetch(url):
    """GET с ретраями и корректной windows-1251 декодировкой."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.encoding = "cp1251"
            if resp.status_code == 200:
                return resp.text
        except requests.RequestException:
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def discover_forum_ids():
    """sitemap.xml содержит один под-sitemap на каждый подфорум (sitemap<id>.xml) -
    это исчерпывающий список всех разделов форума, без ручного перечисления."""
    xml = fetch(f"{BASE_URL}/sitemap.xml")
    if xml is None:
        return []
    ids = sorted(set(int(i) for i in re.findall(r"sitemap(\d+)\.xml", xml)))
    return [i for i in ids if i not in KNOWN_INVALID_FORUM_IDS]


def is_valid_forum_page(html):
    return INVALID_FORUM_MARKER not in html


def get_forum_name(html):
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None


def parse_int(text):
    text = (text or "").strip().replace("\xa0", "").replace(" ", "")
    return int(text) if text.isdigit() else 0


def parse_topic_list_page(html):
    """Возвращает список тем (без текста) со страницы списка тем подфорума."""
    soup = BeautifulSoup(html, "html.parser")
    topics = []
    for row in soup.select("tr"):
        tcl = row.select_one("td.tcl")
        tc2 = row.select_one("td.tc2")
        tc3 = row.select_one("td.tc3")
        if tcl is None or tc2 is None or tc3 is None:
            continue
        link = tcl.select_one("div.tclcon > a[href*='viewtopic.php']")
        if link is None:
            continue
        href = link["href"]
        topic_id = href.split("id=")[-1].split("&")[0].split("#")[0]
        if not topic_id.isdigit():
            continue
        topics.append(
            {
                "topic_id": topic_id,
                "title": link.get_text(strip=True),
                "replies": parse_int(tc2.get_text()),
                "views": parse_int(tc3.get_text()),
            }
        )
    return topics


def has_next_page(html, current_page):
    soup = BeautifulSoup(html, "html.parser")
    pagelink = soup.select_one("div.pagelink")
    if pagelink is None:
        return False
    for a in pagelink.select("a"):
        href = a.get("href", "")
        if f"p={current_page + 1}" in href:
            return True
    return False


def collect_forum_topics(forum_id, forum_name):
    """Обходит все страницы списка тем подфорума, возвращает список dict."""
    topics = []
    page = 1
    while True:
        url = f"{BASE_URL}/viewforum.php?id={forum_id}&p={page}"
        html = fetch(url)
        time.sleep(RATE_LIMIT_SEC)
        if html is None:
            break
        page_topics = parse_topic_list_page(html)
        for t in page_topics:
            t["forum_id"] = forum_id
            t["forum_name"] = forum_name
        topics.extend(page_topics)
        if not has_next_page(html, page):
            break
        page += 1
    return topics


def parse_topic_page(html):
    """Извлекает конкатенированный текст постов первой страницы темы и timestamp создания."""
    soup = BeautifulSoup(html, "html.parser")
    posts = soup.select("div.post")
    texts = []
    created_ts = None
    for post in posts:
        if created_ts is None:
            posted_attr = post.get("data-posted")
            if posted_attr and posted_attr.isdigit():
                created_ts = int(posted_attr)
        content = post.select_one(".post-content")
        if content is not None:
            texts.append(content.get_text(" ", strip=True))
    full_text = " ".join(texts)[:MAX_TEXT_CHARS]
    return full_text, created_ts


def load_done_topic_ids(csv_path):
    if not os.path.exists(csv_path):
        return set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        return {row["topic_id"] for row in csv.DictReader(f)}


def append_row(csv_path, fieldnames, row):
    file_exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def stage1_collect_metadata():
    """Собирает метаданные тем (без текста) по ВСЕМ подфорумам сайта, дозаписывая CSV.

    Список подфорумов берётся из sitemap.xml (см. discover_forum_ids), а не из
    заранее заданного списка - так покрывается весь сайт, а не выборка разделов.
    """
    done_forums = set()
    if os.path.exists(TOPICS_META_CSV):
        with open(TOPICS_META_CSV, newline="", encoding="utf-8") as f:
            done_forums = {int(row["forum_id"]) for row in csv.DictReader(f)}

    forum_ids = discover_forum_ids()
    print(f"[meta] discovered {len(forum_ids)} candidate forums from sitemap.xml")

    for forum_id in forum_ids:
        if forum_id in done_forums:
            print(f"[meta] forum {forum_id} already collected, skip")
            continue

        first_page_html = fetch(f"{BASE_URL}/viewforum.php?id={forum_id}")
        time.sleep(RATE_LIMIT_SEC)
        if first_page_html is None or not is_valid_forum_page(first_page_html):
            print(f"[meta] forum {forum_id}: invalid or unreachable, skip")
            continue

        forum_name = get_forum_name(first_page_html) or f"forum-{forum_id}"
        print(f"[meta] collecting forum {forum_id} ({forum_name}) ...")

        topics = parse_topic_list_page(first_page_html)
        page = 1
        html = first_page_html
        while has_next_page(html, page):
            page += 1
            html = fetch(f"{BASE_URL}/viewforum.php?id={forum_id}&p={page}")
            time.sleep(RATE_LIMIT_SEC)
            if html is None:
                break
            topics.extend(parse_topic_list_page(html))

        for t in topics:
            t["forum_id"] = forum_id
            t["forum_name"] = forum_name
            append_row(TOPICS_META_CSV, META_FIELDS, t)
        print(f"[meta] forum {forum_id} ({forum_name}): {len(topics)} topics")


def stage2_collect_text():
    """По метаданным тем скачивает текст и дату создания, дозаписывая итоговый CSV."""
    with open(TOPICS_META_CSV, newline="", encoding="utf-8") as f:
        all_topics = list(csv.DictReader(f))

    done_ids = load_done_topic_ids(TOPICS_FULL_CSV)
    total = len(all_topics)
    for i, t in enumerate(all_topics, 1):
        if t["topic_id"] in done_ids:
            continue
        url = f"{BASE_URL}/viewtopic.php?id={t['topic_id']}"
        html = fetch(url)
        time.sleep(RATE_LIMIT_SEC)
        if html is None:
            print(f"[text] {i}/{total} topic {t['topic_id']}: FAILED, skipping")
            continue
        text, created_ts = parse_topic_page(html)
        row = dict(t)
        row["text"] = text
        row["created_ts"] = created_ts or ""
        row["scraped_ts"] = int(time.time())
        append_row(TOPICS_FULL_CSV, FULL_FIELDS, row)
        if i % 50 == 0:
            print(f"[text] {i}/{total} topics processed")


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    stage1_collect_metadata()
    stage2_collect_text()
    print("Done.")
