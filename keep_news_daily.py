#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Keep 行业晨报推送脚本 v2
==============================================================
运行模式（通过环境变量 RUN_MODE 控制）：
  fetch：每晚19:00抓取文章，筛选好5条，缓存到 GitHub Gist
  push ：每早9:30从 Gist 读取昨晚缓存，直接推送到飞书

这样即使早上 WeWe RSS 掉线，晨报也能正常推送。
==============================================================
"""

import os
import re
import json
import time
import random
import datetime
import logging
import requests
import feedparser
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S"
)

# ===================== 配置 =====================
FEISHU_WEBHOOK       = os.getenv("FEISHU_WEBHOOK")
FEISHU_ALERT_WEBHOOK = os.getenv("FEISHU_ALERT_WEBHOOK")
GIST_TOKEN           = os.getenv("AI_NEWS_GIST_TOKEN", "")
RUN_MODE             = os.getenv("RUN_MODE", "fetch").lower()

WEWE_RSS_BASE       = "http://82.156.247.106:4000/feeds"
GLOBAL_TIMEOUT      = 20
DEDUP_GIST_FILENAME = "keep_news_pushed_urls.json"
CACHE_GIST_FILENAME = "keep_news_cache.json"
DEDUP_KEEP_DAYS     = 7

# ===================== 公众号配置 =====================
WECHAT_SOURCES = [
    ("机器之心",       "MP_WXS_3073282833", "AI动态",   "ai"),
    ("InfoQ",          "MP_WXS_2390142780", "AI动态",   "ai"),
    ("腾讯科技",       "MP_WXS_2756372660", "AI动态",   "ai"),
    ("晚点LatePost",   "MP_WXS_3572959446", "AI动态",   "ai"),
    ("创业邦",         "MP_WXS_2399032780", "营销运营", "mkt"),
    ("第一新声",       "MP_WXS_3917197004", "营销运营", "mkt"),
    ("深响",           "MP_WXS_3539784411", "营销运营", "mkt"),
    ("精练GymSquare",  "MP_WXS_3556622257", "健身运动", "fit"),
    ("快消品渠道管理", "MP_WXS_2395909425", "健身运动", "fit"),
    ("重构零售实验室", "MP_WXS_3898208553", "零售品牌", "retail"),
]

SLOT_TARGETS = {
    "AI动态":   2,
    "营销运营": 1,
    "健身运动": 1,
    "零售品牌": 1,
}

KEEP_KEYWORDS = [
    "ai", "人工智能", "大模型", "llm", "gpt", "claude", "gemini", "智能",
    "算法", "机器学习", "深度学习", "chatgpt", "agent", "自动化",
    "商业化", "会员", "订阅", "增长", "用户", "营销", "品牌", "运营",
    "变现", "roi", "转化", "私域", "流量", "内容", "种草",
    "健身", "运动", "keep", "lululemon", "nike", "adidas", "瑜伽", "跑步",
    "训练", "健康", "体育", "穿戴", "手环", "手表", "蛋白", "营养",
    "零售", "电商", "ip", "联名", "潮牌", "消费", "drg", "dtc",
    "渠道", "供应链", "线下", "线上", "复购", "盈利", "营收",
    "openai", "anthropic", "google", "microsoft", "meta", "字节", "腾讯",
    "阿里", "百度", "小米", "华为", "苹果", "特斯拉",
]

AI_KEYWORDS = [
    "ai", "人工智能", "大模型", "llm", "gpt", "claude", "gemini",
    "deepseek", "openai", "anthropic", "模型", "智能体", "agent",
    "机器学习", "深度学习", "神经网络", "算法", "推理", "训练"
]

# ===================== 工具函数 =====================
def get_today():
    return datetime.date.today().strftime("%Y-%m-%d")

def get_yesterday():
    return (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

def clean_text(text, max_len=None):
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', str(text)).strip()
    if max_len and len(text) > max_len:
        return text[:max_len]
    return text

def strip_html(raw_html):
    if not raw_html:
        return ""
    return clean_text(BeautifulSoup(str(raw_html), "html.parser").get_text())

def is_relevant(title, summary=""):
    text = (title + " " + summary[:300]).lower()
    return any(kw in text for kw in KEEP_KEYWORDS)

# ===================== Gist 通用操作 =====================
def _get_gist_id(filename):
    if not GIST_TOKEN:
        return None
    try:
        resp = requests.get(
            "https://api.github.com/gists",
            headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"},
            timeout=15
        )
        for gist in resp.json():
            if filename in gist.get("files", {}):
                return gist["id"]
    except Exception as e:
        logging.warning(f"[Gist] 获取列表失败: {e}")
    return None

def gist_read(filename):
    gist_id = _get_gist_id(filename)
    if not gist_id:
        return None, None
    try:
        resp = requests.get(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"},
            timeout=15
        )
        content = resp.json()["files"][filename]["content"]
        return json.loads(content), gist_id
    except Exception as e:
        logging.warning(f"[Gist] 读取失败 {filename}: {e}")
        return None, gist_id

def gist_write(filename, data, gist_id=None):
    content = json.dumps(data, ensure_ascii=False, indent=2)
    headers = {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        if gist_id:
            requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                headers=headers,
                json={"files": {filename: {"content": content}}},
                timeout=15
            )
        else:
            requests.post(
                "https://api.github.com/gists",
                headers=headers,
                json={"public": False, "files": {filename: {"content": content}}},
                timeout=15
            )
        logging.info(f"[Gist] 写入成功: {filename}")
    except Exception as e:
        logging.warning(f"[Gist] 写入失败 {filename}: {e}")

# ===================== 去重 =====================
def load_pushed_urls():
    data, gist_id = gist_read(DEDUP_GIST_FILENAME)
    if not data:
        return set(), gist_id
    cutoff = (datetime.date.today() - datetime.timedelta(days=DEDUP_KEEP_DAYS)).strftime("%Y-%m-%d")
    filtered = {d: urls for d, urls in data.items() if d >= cutoff}
    all_urls = set()
    for urls in filtered.values():
        all_urls.update(urls)
    logging.info(f"[去重] 已加载 {len(all_urls)} 条历史URL")
    return all_urls, gist_id

def save_pushed_urls(new_urls, gist_id):
    if not new_urls:
        return
    today = get_today()
    data, _ = gist_read(DEDUP_GIST_FILENAME)
    existing = data or {}
    existing.setdefault(today, [])
    for url in new_urls:
        if url and url not in existing[today]:
            existing[today].append(url)
    cutoff = (datetime.date.today() - datetime.timedelta(days=DEDUP_KEEP_DAYS)).strftime("%Y-%m-%d")
    existing = {d: v for d, v in existing.items() if d >= cutoff}
    gist_write(DEDUP_GIST_FILENAME, existing, gist_id)
    logging.info(f"[去重] 已保存今日 {len(new_urls)} 条URL")

# ===================== 缓存读写 =====================
def save_cache(articles):
    _, gist_id = gist_read(CACHE_GIST_FILENAME)
    data = {"date": get_today(), "articles": articles}
    gist_write(CACHE_GIST_FILENAME, data, gist_id)
    logging.info(f"[缓存] 已保存 {len(articles)} 条文章")

def load_cache():
    data, _ = gist_read(CACHE_GIST_FILENAME)
    if not data:
        logging.warning("[缓存] 未找到缓存数据")
        return None
    cache_date = data.get("date", "")
    if cache_date not in [get_yesterday(), get_today()]:
        logging.warning(f"[缓存] 缓存日期 {cache_date} 已过期")
        return None
    articles = data.get("articles", [])
    logging.info(f"[缓存] 读取到 {len(articles)} 条文章（缓存日期：{cache_date}）")
    return articles

# ===================== 抓取公众号文章 =====================
def fetch_wechat_summary(url):
    if not url or "mp.weixin.qq.com" not in url:
        return ""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        content_el = soup.find("div", id="js_content") or soup.find("div", class_="rich_media_content")
        if content_el:
            paras = [p.get_text(" ", strip=True) for p in content_el.find_all("p") if len(p.get_text(strip=True)) > 20]
            text = " ".join(paras[:5])
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:150] + "..." if len(text) > 150 else text
    except Exception as e:
        logging.warning(f"  [摘要抓取] 失败: {e}")
    return ""

def fetch_articles_from_source(name, mp_id, category, tag, pushed_urls):
    rss_url = f"{WEWE_RSS_BASE}/{mp_id}.atom"
    articles = []
    try:
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            logging.warning(f"⚠️ {name}: RSS无内容")
            return articles

        for entry in feed.entries[:20]:
            title    = clean_text(getattr(entry, "title", ""))
            link     = getattr(entry, "link", "")
            pub_time = getattr(entry, "published", "") or getattr(entry, "updated", "")

            summary = ""
            if hasattr(entry, "content") and entry.content:
                summary = strip_html(entry.content[0].get("value", ""))
            if not summary or len(summary) < 20:
                summary = strip_html(getattr(entry, "summary", ""))
            if not summary or len(summary) < 20:
                summary = strip_html(getattr(entry, "description", ""))
            if not summary or len(summary) < 20:
                summary = title

            summary = re.sub(r'^(摘要[：:]|导读[：:]|编者按[：:])', '', summary).strip()
            summary = summary[:150] + "..." if len(summary) > 150 else summary

            if not summary or summary == title:
                fetched = fetch_wechat_summary(link)
                if fetched:
                    summary = fetched

            if len(title) < 5:
                continue
            if link and link in pushed_urls:
                logging.info(f"  [去重] 跳过已推送: {title[:40]}")
                continue

            actual_tag      = tag
            actual_category = category
            if any(kw in title.lower() for kw in AI_KEYWORDS):
                actual_tag      = "ai"
                actual_category = "AI动态"

            if not is_relevant(title, summary):
                logging.info(f"  [过滤] 与Keep业务无关: {title[:40]}")
                continue

            articles.append({
                "title":    title,
                "link":     link,
                "summary":  summary,
                "source":   name,
                "category": actual_category,
                "tag":      actual_tag,
                "pub_time": pub_time,
            })

        logging.info(f"✅ {name}: 获取 {len(articles)} 条候选文章")
    except Exception as e:
        logging.error(f"❌ {name}: 抓取失败 {e}")
    return articles

# ===================== 告警 =====================
def send_alert(message):
    if not FEISHU_ALERT_WEBHOOK:
        logging.warning("⚠️ FEISHU_ALERT_WEBHOOK 未配置")
        return
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "⚠️ Keep行业晨报 系统告警"},
                "template": "red"
            },
            "elements": [{
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**时间**：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n**告警内容**：{message}"
                }
            }]
        }
    }
    try:
        resp = requests.post(FEISHU_ALERT_WEBHOOK, json=payload, timeout=15)
        if resp.status_code == 200 and resp.json().get("StatusCode") == 0:
            logging.info("✅ 告警推送成功")
        else:
            logging.error(f"❌ 告警推送失败: {resp.text[:100]}")
    except Exception as e:
        logging.error(f"❌ 告警推送异常: {e}")

# ===================== 飞书推送 =====================
BADGE_COLORS = {
    "ai":     {"text": "#185FA5", "label": "【⚡ AI动态】"},
    "mkt":    {"text": "#3C3489", "label": "【📊 营销运营】"},
    "fit":    {"text": "#3B6D11", "label": "【🏃 健身运动】"},
    "retail": {"text": "#854F0B", "label": "【🎯 零售品牌】"},
}

IDX_EMOJI = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣"}

def send_to_feishu(articles):
    if not FEISHU_WEBHOOK:
        logging.error("❌ FEISHU_WEBHOOK 未配置")
        return
    if not articles:
        logging.error("❌ 没有文章可推送")
        return

    elements = []
    for idx, article in enumerate(articles, 1):
        title     = article.get("title", "无标题")
        summary   = article.get("summary", "暂无摘要")
        source    = article.get("source", "未知来源")
        link      = article.get("link", "#")
        tag       = article.get("tag", "ai")
        color     = BADGE_COLORS.get(tag, BADGE_COLORS["ai"])
        num_emoji = IDX_EMOJI.get(idx, f"{idx}.")

        card_elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{num_emoji} {title}**\n"
                        f"<font color='{color['text']}'>{color['label']}</font>　{source}\n\n"
                        f"{summary}"
                    )
                }
            },
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看原文"},
                    "type": "default",
                    "url": link
                }]
            },
            {"tag": "hr"}
        ]
        elements.extend(card_elements)

    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"Keep 行业晨报 · {get_today()}"},
                "template": "indigo"
            },
            "elements": elements
        }
    }

    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=15)
        if resp.status_code == 200 and resp.json().get("StatusCode") == 0:
            logging.info("✅ 飞书推送成功")
        else:
            logging.error(f"❌ 飞书推送失败: {resp.text[:200]}")
    except Exception as e:
        logging.error(f"❌ 飞书推送异常: {e}")

# ===================== 筛选文章 =====================
def select_articles(pushed_urls):
    category_pool = {cat: [] for cat in SLOT_TARGETS}

    for name, mp_id, category, tag in WECHAT_SOURCES:
        articles = fetch_articles_from_source(name, mp_id, category, tag, pushed_urls)
        category_pool[category].extend(articles)
        time.sleep(random.uniform(0.5, 1.0))

    for cat in category_pool:
        category_pool[cat].sort(key=lambda x: x.get("pub_time", ""), reverse=True)

    total_candidates = sum(len(v) for v in category_pool.values())
    if total_candidates == 0:
        return None

    final = []
    used_links = set()
    used_sources = set()

    for category, target in SLOT_TARGETS.items():
        candidates = category_pool.get(category, [])
        count = 0
        for article in candidates:
            if count >= target:
                break
            link   = article.get("link", "")
            source = article.get("source", "")
            if link in used_links:
                continue
            cat_source_key = f"{category}_{source}"
            if cat_source_key in used_sources:
                continue
            final.append(article)
            used_links.add(link)
            used_sources.add(cat_source_key)
            count += 1
            logging.info(f"✅ [{category}] 入选: {article['title'][:50]}")
        if count < target:
            logging.warning(f"⚠️ [{category}] 只找到 {count}/{target} 条")

    if len(final) < 5:
        all_candidates = []
        for candidates in category_pool.values():
            all_candidates.extend(candidates)
        all_candidates.sort(key=lambda x: x.get("pub_time", ""), reverse=True)
        for article in all_candidates:
            if len(final) >= 5:
                break
            if article.get("link", "") not in used_links:
                final.append(article)
                used_links.add(article.get("link", ""))

    return final

# ===================== 主函数 =====================
def main():
    logging.info(f"🚀 Keep 行业晨报 v2 启动，模式：{RUN_MODE.upper()}")
    logging.info(f"📅 今日日期：{get_today()}")

    if RUN_MODE == "fetch":
        logging.info("📥 fetch模式：开始抓取文章并缓存")
        pushed_urls, gist_id = load_pushed_urls()
        final = select_articles(pushed_urls)

        if final is None:
            alert_msg = "所有公众号均未抓取到文章，WeWe RSS 服务可能已掉线。请登录 http://82.156.247.106:4000 检查账号状态并重新扫码登录。"
            logging.error(f"❌ {alert_msg}")
            send_alert(alert_msg)
            return

        if len(final) == 0:
            send_alert("今晚抓取结果为0条，明早晨报将无法推送，请检查 WeWe RSS 服务状态。")
            return
        elif len(final) < 3:
            send_alert(f"今晚仅抓取到 {len(final)} 条内容（少于3条），明早晨报内容不足，请检查 WeWe RSS 服务状态。")

        save_cache(final)
        logging.info(f"✅ fetch完成，已缓存 {len(final)} 条文章")

    elif RUN_MODE == "push":
        logging.info("📤 push模式：从缓存读取文章并推送")
        articles = load_cache()

        if not articles:
            alert_msg = "早上推送时未找到有效缓存，可能是昨晚fetch任务未运行或WeWe RSS掉线导致缓存为空。"
            logging.error(f"❌ {alert_msg}")
            send_alert(alert_msg)
            return

        send_to_feishu(articles)

        pushed_urls, gist_id = load_pushed_urls()
        today_urls = [a.get("link", "") for a in articles if a.get("link")]
        save_pushed_urls(today_urls, gist_id)
        logging.info("🏁 push完成")

    else:
        logging.error(f"❌ 未知的 RUN_MODE：{RUN_MODE}，请设置为 fetch 或 push")


if __name__ == "__main__":
    main()

