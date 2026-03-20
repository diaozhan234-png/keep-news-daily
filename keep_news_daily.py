#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Keep 行业晨报推送脚本 v1
==============================================================
核心逻辑：
  - 从 WeWe RSS 服务抓取10个微信公众号的最新文章
  - 按业务分类槽位筛选（AI动态2条、营销运营1条、健身运动1条、零售品牌1条）
  - 去重：用 GitHub Gist 存储近7天已推送 URL
  - 推送飞书互动卡片，含业务标签、来源、摘要、查看原文按钮
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

WEWE_RSS_BASE  = "http://82.156.247.106:4000/feeds"
GLOBAL_TIMEOUT = 20
DEDUP_GIST_FILENAME = "keep_news_pushed_urls.json"
DEDUP_KEEP_DAYS = 7

# ===================== 公众号配置 =====================
# 格式：(公众号名称, RSS文件名, 业务分类, 分类标签)
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

# 槽位分配：{分类: 目标条数}
SLOT_TARGETS = {
    "AI动态":   2,
    "营销运营": 1,
    "健身运动": 1,
    "零售品牌": 1,
}

# Keep 业务相关关键词（用于过滤明显无关内容）
KEEP_KEYWORDS = [
    # AI/科技
    "ai", "人工智能", "大模型", "llm", "gpt", "claude", "gemini", "智能",
    "算法", "机器学习", "深度学习", "chatgpt", "agent", "自动化",
    # 商业/运营
    "商业化", "会员", "订阅", "增长", "用户", "营销", "品牌", "运营",
    "变现", "roi", "转化", "私域", "流量", "内容", "种草",
    # 健身/运动/消费品
    "健身", "运动", "keep", "lululemon", "nike", "adidas", "瑜伽", "跑步",
    "训练", "健康", "体育", "穿戴", "手环", "手表", "蛋白", "营养",
    # 零售/品牌/IP
    "零售", "电商", "ip", "联名", "潮牌", "消费", "drg", "dtc",
    "渠道", "供应链", "线下", "线上", "复购", "盈利", "营收",
    # 科技公司动态
    "openai", "anthropic", "google", "microsoft", "meta", "字节", "腾讯",
    "阿里", "百度", "小米", "华为", "苹果", "特斯拉",
]

# ===================== 工具函数 =====================
def get_today():
    return datetime.date.today().strftime("%Y-%m-%d")

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
    """判断文章是否与 Keep 业务相关"""
    text = (title + " " + summary[:300]).lower()
    return any(kw in text for kw in KEEP_KEYWORDS)

# ===================== 去重：Gist 存储 =====================
def _get_gist_id():
    if not GIST_TOKEN:
        return None
    try:
        resp = requests.get(
            "https://api.github.com/gists",
            headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"},
            timeout=15
        )
        for gist in resp.json():
            if DEDUP_GIST_FILENAME in gist.get("files", {}):
                return gist["id"]
    except Exception as e:
        logging.warning(f"[去重] 获取Gist列表失败: {e}")
    return None

def load_pushed_urls():
    if not GIST_TOKEN:
        return set(), None
    gist_id = _get_gist_id()
    if not gist_id:
        return set(), None
    try:
        resp = requests.get(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"},
            timeout=15
        )
        raw = resp.json()["files"][DEDUP_GIST_FILENAME]["content"]
        data = json.loads(raw)
        cutoff = (datetime.date.today() - datetime.timedelta(days=DEDUP_KEEP_DAYS)).strftime("%Y-%m-%d")
        filtered = {d: urls for d, urls in data.items() if d >= cutoff}
        all_urls = set()
        for urls in filtered.values():
            all_urls.update(urls)
        logging.info(f"[去重] 已加载 {len(all_urls)} 条历史URL（近{DEDUP_KEEP_DAYS}天）")
        return all_urls, gist_id
    except Exception as e:
        logging.warning(f"[去重] 加载历史URL失败: {e}")
        return set(), gist_id

def save_pushed_urls(new_urls, gist_id):
    if not GIST_TOKEN or not new_urls:
        return
    today = get_today()
    existing = {}
    if gist_id:
        try:
            resp = requests.get(
                f"https://api.github.com/gists/{gist_id}",
                headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"},
                timeout=15
            )
            existing = json.loads(resp.json()["files"][DEDUP_GIST_FILENAME]["content"])
        except Exception:
            pass
    existing.setdefault(today, [])
    for url in new_urls:
        if url and url not in existing[today]:
            existing[today].append(url)
    cutoff = (datetime.date.today() - datetime.timedelta(days=DEDUP_KEEP_DAYS)).strftime("%Y-%m-%d")
    existing = {d: v for d, v in existing.items() if d >= cutoff}
    content = json.dumps(existing, ensure_ascii=False, indent=2)
    try:
        if gist_id:
            requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"},
                json={"files": {DEDUP_GIST_FILENAME: {"content": content}}},
                timeout=15
            )
        else:
            requests.post(
                "https://api.github.com/gists",
                headers={"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"},
                json={"public": False, "files": {DEDUP_GIST_FILENAME: {"content": content}}},
                timeout=15
            )
        logging.info(f"[去重] 已保存今日 {len(new_urls)} 条URL到Gist")
    except Exception as e:
        logging.warning(f"[去重] 保存URL失败: {e}")

# ===================== 抓取公众号文章 =====================
def fetch_articles_from_source(name, mp_id, category, tag, pushed_urls):
    """从 WeWe RSS 抓取单个公众号的文章列表"""
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

            # 多级摘要获取：content → summary → description → title兜底
            summary = ""
            if hasattr(entry, "content") and entry.content:
                summary = strip_html(entry.content[0].get("value", ""))
            if not summary or len(summary) < 20:
                summary = strip_html(getattr(entry, "summary", ""))
            if not summary or len(summary) < 20:
                summary = strip_html(getattr(entry, "description", ""))
            if not summary or len(summary) < 20:
                summary = title

            # 清理微信公众号常见无用前缀
            summary = re.sub(r'^(摘要[：:]|导读[：:]|编者按[：:])', '', summary).strip()
            summary = summary[:150] + "..." if len(summary) > 150 else summary

            # 摘要仍为空则抓取原文
            if not summary or summary == title:
                fetched = fetch_wechat_summary(link)
                if fetched:
                    summary = fetched
                    logging.info(f"  [摘要] 抓取原文成功: {title[:30]}")

            if len(title) < 5:
                continue
            if link and link in pushed_urls:
                logging.info(f"  [去重] 跳过已推送: {title[:40]}")
                continue

            # 动态分类：AI相关关键词命中时强制归为AI动态
            actual_tag      = tag
            actual_category = category
            ai_keywords = [
                "ai", "人工智能", "大模型", "llm", "gpt", "claude", "gemini",
                "deepseek", "openai", "anthropic", "模型", "智能体", "agent",
                "机器学习", "深度学习", "神经网络", "算法", "推理", "训练"
            ]
            title_lower = title.lower()
            if any(kw in title_lower for kw in ai_keywords):
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

def fetch_wechat_summary(url):
    """抓取微信公众号文章正文前150字作为摘要"""
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
        # 微信文章正文容器
        content_el = soup.find("div", id="js_content") or soup.find("div", class_="rich_media_content")
        if content_el:
            paras = [p.get_text(" ", strip=True) for p in content_el.find_all("p") if len(p.get_text(strip=True)) > 20]
            text = " ".join(paras[:5])
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:150] + "..." if len(text) > 150 else text
    except Exception as e:
        logging.warning(f"  [摘要抓取] 失败: {e}")
    return ""


def send_alert(message):
    """推送告警消息到专用告警群"""
    if not FEISHU_ALERT_WEBHOOK:
        logging.warning("⚠️ FEISHU_ALERT_WEBHOOK 未配置，告警无法发送")
        return
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "⚠️ Keep行业晨报 系统告警"},
                "template": "red"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**时间**：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n**告警内容**：{message}"
                    }
                }
            ]
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
    "ai":     {"bg": "#E6F1FB", "text": "#185FA5", "label": "【⚡ AI动态】"},
    "mkt":    {"bg": "#EEEDFE", "text": "#3C3489", "label": "【📊 营销运营】"},
    "fit":    {"bg": "#EAF3DE", "text": "#3B6D11", "label": "【🏃 健身运动】"},
    "retail": {"bg": "#FAEEDA", "text": "#854F0B", "label": "【🎯 零售品牌】"},
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
        title    = article.get("title", "无标题")
        summary  = article.get("summary", "暂无摘要")
        source   = article.get("source", "未知来源")
        link     = article.get("link", "#")
        tag      = article.get("tag", "ai")
        color    = BADGE_COLORS.get(tag, BADGE_COLORS["ai"])
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
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看原文"},
                        "type": "default",
                        "url": link
                    }
                ]
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
                "title": {
                    "tag": "plain_text",
                    "content": f"Keep 行业晨报 · {get_today()}"
                },
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

# ===================== 主函数 =====================
def main():
    logging.info("🚀 Keep 行业晨报 v1 启动")
    logging.info(f"📅 今日日期：{get_today()}")

    # 加载历史已推送 URL
    pushed_urls, gist_id = load_pushed_urls()

    # 按分类收集候选文章
    category_pool = {cat: [] for cat in SLOT_TARGETS}

    for name, mp_id, category, tag in WECHAT_SOURCES:
        articles = fetch_articles_from_source(name, mp_id, category, tag, pushed_urls)
        category_pool[category].extend(articles)
        time.sleep(random.uniform(0.5, 1.0))

    # 按发布时间排序（最新的优先）
    for cat in category_pool:
        category_pool[cat].sort(key=lambda x: x.get("pub_time", ""), reverse=True)

    # 健康检查：所有公众号都返回0条说明 WeWe RSS 可能掉线
    total_candidates = sum(len(v) for v in category_pool.values())
    if total_candidates == 0:
        alert_msg = "所有公众号均未抓取到文章，WeWe RSS 服务可能已掉线。请登录 http://82.156.247.106:4000 检查账号状态并重新扫码登录。"
        logging.error(f"❌ {alert_msg}")
        send_alert(alert_msg)
        return

    # 按槽位取文章，每个公众号最多贡献1条，确保来源多样性
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
            # 同一公众号在同一分类内最多贡献1条
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

    # 不足5条时从剩余候选里补
    if len(final) < 5:
        logging.warning(f"⚠️ 当前 {len(final)} 条，尝试补足至5条")
        all_candidates = []
        for candidates in category_pool.values():
            all_candidates.extend(candidates)
        all_candidates.sort(key=lambda x: x.get("pub_time", ""), reverse=True)
        for article in all_candidates:
            if len(final) >= 5:
                break
            link = article.get("link", "")
            if link not in used_links:
                final.append(article)
                used_links.add(link)

    logging.info(f"📋 最终推送 {len(final)} 条")

    if len(final) == 0:
        send_alert("今日晨报抓取结果为0条，未推送任何内容，请检查 WeWe RSS 服务状态。")
        return
    elif len(final) < 3:
        send_alert(f"今日晨报仅抓取到 {len(final)} 条内容（少于3条），请检查 WeWe RSS 服务或公众号订阅状态。")

    # 推送飞书
    send_to_feishu(final)

    # 保存已推送 URL
    today_urls = [a.get("link", "") for a in final if a.get("link")]
    save_pushed_urls(today_urls, gist_id)

    logging.info("🏁 任务完成")


if __name__ == "__main__":
    main()
