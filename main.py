"""
astrbot_plugin_vrpspdouchong - VR/PSP douchong plugin.

Migrated from nonebot-plugin-vrpspdouchong.
Commands: VR/PSP douchong rankings, live list, live sessions, SC records, fan counts, financial overview.
Results rendered as images via Pillow.
"""
from __future__ import annotations

import base64
import re
import time
import datetime
import tempfile
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import httpx
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Nodes, Node, Image, Plain
from astrbot.api.star import Context, Star, register

from .toolkit import PicGenerator, Color, timestamp_format


# ============================================================================
# Utility: month parsing
# ============================================================================
def current_month_code() -> str:
    return time.strftime("%Y%m", time.localtime())


_MONTH_RE_1 = re.compile(r"^(\d{4})(\d{2})$")
_MONTH_RE_2 = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")


def normalize_month_arg(raw: str) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip()
    m1 = _MONTH_RE_1.fullmatch(text)
    m2 = _MONTH_RE_2.fullmatch(text)
    if m1:
        yyyy, mm = m1.group(1), m1.group(2)
    elif m2:
        yyyy, mm = m2.group(1), m2.group(2)
    else:
        return None
    try:
        mm_i = int(mm)
        if 1 <= mm_i <= 12:
            return f"{yyyy}{mm}"
    except Exception:
        return None
    return None


def build_year_month_codes(year: int, now_dt: Optional[datetime.datetime] = None) -> Optional[List[str]]:
    current = now_dt or datetime.datetime.now()
    current_year = current.year
    current_month = current.month
    if year > current_year:
        return None
    end_month = current_month if year == current_year else 12
    return [f"{year}{m:02d}" for m in range(1, end_month + 1)]


def normalize_period_arg(raw: str) -> Optional[Tuple[List[str], str]]:
    text = (raw or "").strip()
    if not text:
        month_code = current_month_code()
        return [month_code], f"{month_code[:4]}-{month_code[4:]}"
    month_code = normalize_month_arg(text)
    if month_code:
        return [month_code], f"{month_code[:4]}-{month_code[4:]}"
    y = _YEAR_RE.fullmatch(text)
    if not y:
        return None
    year = int(y.group(1))
    month_codes = build_year_month_codes(year)
    if not month_codes:
        return None
    end_month = int(month_codes[-1][4:])
    return month_codes, f"{year}年 1-{end_month}月累计"


# ============================================================================
# Utility: numeric
# ============================================================================
def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _safe_str(v: Any) -> str:
    return "" if v is None else str(v)


# ============================================================================
# Utility: time formatting
# ============================================================================
def _parse_dt(s: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _sec_to_hms(total_seconds: int) -> str:
    if total_seconds < 0:
        total_seconds = 0
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _duration_to_seconds(hms: Any) -> int:
    try:
        parts = str(hms).split(":")
        if len(parts) != 3:
            return 0
        h, m, s = map(int, parts)
        return max(0, h * 3600 + m * 60 + s)
    except Exception:
        return 0


def _seconds_to_duration(total_seconds: int) -> str:
    sec = max(0, int(total_seconds))
    hh = sec // 3600
    mm = (sec % 3600) // 60
    ss = sec % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def format_duration(hms: str) -> str:
    try:
        parts = str(hms).split(":")
        if len(parts) == 3:
            h, m, s = map(int, parts)
            total_hours = h + m / 60 + s / 3600
            return f"{total_hours:.1f}h"
    except Exception:
        pass
    return str(hms)


def format_fans(attention: int) -> str:
    try:
        if attention >= 10000:
            return f"{attention / 10000:.1f}w"
        return str(attention)
    except Exception:
        return str(attention)


def format_count(v: Optional[int]) -> str:
    if v is None:
        return "-"
    try:
        return str(int(v))
    except Exception:
        return "-"


def format_hourly_rate(total: Any, live_duration: Any) -> str:
    duration_seconds = _duration_to_seconds(live_duration)
    if duration_seconds <= 0:
        return "0.00"
    hourly_rate = _to_float(total, 0.0) / (duration_seconds / 3600)
    return f"{hourly_rate:.2f}"


# ============================================================================
# Utility: live duration calc
# ============================================================================
def calc_live_duration_with_live_time(
    live_duration: Any, live_time: Any, now_ts: Optional[int] = None
) -> str:
    try:
        duration_parts = str(live_duration).split(":")
        if len(duration_parts) != 3:
            return str(live_duration)
        h, m, s = map(int, duration_parts)
        base_seconds = h * 3600 + m * 60 + s
        live_dt = datetime.datetime.strptime(str(live_time), "%Y-%m-%d %H:%M:%S")
        live_ts = int(live_dt.timestamp())
        now_seconds = int(now_ts if now_ts is not None else time.time())
        total_seconds = max(0, now_seconds - live_ts + base_seconds)
        hh = total_seconds // 3600
        mm = (total_seconds % 3600) // 60
        ss = total_seconds % 60
        return f"{hh:02d}:{mm:02d}:{ss:02d}"
    except Exception:
        return str(live_duration)


def apply_live_duration_calc(data_list: List[Dict[str, Any]], now_ts: Optional[int] = None) -> None:
    current_ts = int(now_ts if now_ts is not None else time.time())
    for d in data_list:
        if not isinstance(d, dict):
            continue
        d["live_duration"] = calc_live_duration_with_live_time(
            d.get("live_duration", "00:00:00"), d.get("live_time", ""), current_ts
        )


def _is_current_year_period(month_codes: List[str]) -> bool:
    if not month_codes:
        return False
    current_year = datetime.datetime.now().year
    return all(_to_int(code[:4], 0) == current_year for code in month_codes)


# ============================================================================
# Utility: anchor search
# ============================================================================
def _match_anchor(items: List[Dict[str, Any]], keyword: str) -> Optional[Dict[str, Any]]:
    return next((it for it in items if keyword in str(it.get("anchor_name", ""))), None)


# ============================================================================
# Utility: build query source text (AstrBot style)
# ============================================================================
def build_query_source_text(event: AstrMessageEvent) -> str:
    uid = event.unified_msg_origin
    gid = event.get_group_id() or "unknown group"
    return f"由群{gid}中{uid}查询"


# ============================================================================
# Plugin
# ============================================================================
@register(
    "astrbot_plugin_vrpspdouchong",
    "QianQiuZy (AstrBot migration)",
    "VR/PSP douchong plugin: VR/PSP gift rankings, live lists, live sessions, SC records, fan counts, financial overview",
    "1.0.0",
    "https://github.com/QianQiuZy/nonebot-plugin-vrpspdouchong",
)
class VRPSPDouchong(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.cfg = config

    # ========================= 1. Douchong =========================
    @filter.command("VRdou", alias={"VR斗虫", "vr斗虫", "vrdou"})
    async def vr_douchong(self, event: AstrMessageEvent):
        arg = self._get_cmd_arg(event)
        async for seg in self._handle_douchong(
            event, arg,
            api_base=self.cfg.get("vr_gift_api_base", "https://vr.qianqiuzy.cn/gift"),
            title=self.cfg.get("vr_douchong_title", "VR Douchong"),
        ):
            yield seg

    @filter.command("PSPdou", alias={"PSP斗虫", "psp斗虫", "pspdou"})
    async def psp_douchong(self, event: AstrMessageEvent):
        arg = self._get_cmd_arg(event)
        async for seg in self._handle_douchong(
            event, arg,
            api_base=self.cfg.get("psp_gift_api_base", "https://psp.qianqiuzy.cn/gift"),
            title=self.cfg.get("psp_douchong_title", "PSP Douchong"),
        ):
            yield seg

    @filter.command("dou", alias={"大乱斗斗虫", "dou"})
    async def brawl_douchong(self, event: AstrMessageEvent):
        arg = self._get_cmd_arg(event)
        async for seg in self._handle_douchong_brawl(event, arg):
            yield seg

    # ========================= 2. Live list =========================
    @filter.command("VRlive", alias={"VR开播", "vr开播"})
    async def vr_live(self, event: AstrMessageEvent):
        async for seg in self._handle_live_list(
            event,
            api_url=self.cfg.get("vr_gift_api_base", "https://vr.qianqiuzy.cn/gift"),
            title=self.cfg.get("vr_live_title", "VR Live"),
        ):
            yield seg

    @filter.command("PSPlive", alias={"PSP开播", "psp开播"})
    async def psp_live(self, event: AstrMessageEvent):
        async for seg in self._handle_live_list(
            event,
            api_url=self.cfg.get("psp_gift_api_base", "https://psp.qianqiuzy.cn/gift"),
            title=self.cfg.get("psp_live_title", "PSP Live"),
        ):
            yield seg

    @filter.command("brawllive", alias={"大乱斗开播"})
    async def brawl_live(self, event: AstrMessageEvent):
        async for seg in self._handle_live_list_brawl(event):
            yield seg

    # ========================= 3. Query =========================
    @filter.command("live", alias={"查直播"})
    async def query_live(self, event: AstrMessageEvent):
        arg = self._get_cmd_arg(event)
        async for seg in self._handle_query_live(event, arg):
            yield seg

    @filter.command("sc", alias={"查SC", "查sc"})
    async def query_sc(self, event: AstrMessageEvent):
        arg = self._get_cmd_arg(event)
        async for seg in self._handle_query_sc(event, arg):
            yield seg

    @filter.command("fans", alias={"查粉丝", "查粉"})
    async def query_attention(self, event: AstrMessageEvent):
        arg = self._get_cmd_arg(event)
        async for seg in self._handle_query_attention(event, arg):
            yield seg

    @filter.command("income", alias={"查流水"})
    async def query_financial(self, event: AstrMessageEvent):
        arg = self._get_cmd_arg(event)
        async for seg in self._handle_query_financial(event, arg):
            yield seg

    # ===================== Helpers =====================
    @staticmethod
    def _get_cmd_arg(event: AstrMessageEvent) -> str:
        msg = event.get_message_str().strip()
        parts = msg.split(None, 1)
        if len(parts) > 1:
            return parts[1].strip()
        return ""

    # ===================== HTTP Helpers =====================
    @property
    def _timeout(self) -> float:
        return float(self.cfg.get("vr_http_timeout", 5))

    async def _fetch_json(self, url: str) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def _fetch_month_data(self, api_base: str, month_code: str) -> List[Dict[str, Any]]:
        url = f"{api_base}/by_month?month={month_code}"
        data = await self._fetch_json(url)
        if not isinstance(data, list):
            raise ValueError("API returned non-list data")
        return [d for d in data if isinstance(d, dict)]

    async def _fetch_period_data(self, api_base: str, month_codes: List[str]) -> List[Dict[str, Any]]:
        if len(month_codes) == 1:
            return await self._fetch_month_data(api_base, month_codes[0])
        all_rows: List[Dict[str, Any]] = []
        for mc in month_codes:
            all_rows.extend(await self._fetch_month_data(api_base, mc))
        include_live_status = _is_current_year_period(month_codes)
        return self._merge_monthly_data(all_rows, include_live_status=include_live_status)

    @staticmethod
    def _merge_monthly_data(all_rows: List[Dict[str, Any]], *, include_live_status: bool = False) -> List[Dict[str, Any]]:
        numeric_sum_fields = [
            "effective_days", "guard_1", "guard_2", "guard_3", "fans_count",
            "blind_box_count", "blind_box_profit", "gift", "super_chat", "guard",
        ]
        int_sum_fields = ["effective_days", "guard_1", "guard_2", "guard_3", "fans_count", "blind_box_count"]
        max_fields = ["attention"]
        merged: Dict[str, Dict[str, Any]] = {}
        for row in all_rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("room_id") or row.get("anchor_name") or "")
            if not key:
                continue
            if key not in merged:
                base = dict(row)
                for f in numeric_sum_fields:
                    base[f] = 0
                for f in max_fields:
                    base[f] = 0
                base["status"] = 0
                base["live_duration_seconds"] = 0
                base["live_duration"] = "00:00:00"
                base["live_time"] = "0000-00-00 00:00:00"
                merged[key] = base
            target = merged[key]
            for f in numeric_sum_fields:
                target[f] = _to_float(target.get(f, 0)) + _to_float(row.get(f, 0))
            for f in int_sum_fields:
                target[f] = _to_int(round(_to_float(target.get(f, 0))))
            for f in max_fields:
                target[f] = max(_to_int(target.get(f, 0)), _to_int(row.get(f, 0)))
            target["live_duration_seconds"] = _to_int(target.get("live_duration_seconds", 0)) + _duration_to_seconds(
                row.get("live_duration", "00:00:00")
            )
            target["live_duration"] = _seconds_to_duration(target["live_duration_seconds"])
            if include_live_status and _to_int(row.get("status", 0)) == 1:
                target["status"] = 1
                target["live_time"] = str(row.get("live_time") or target.get("live_time") or "0000-00-00 00:00:00")
        for v in merged.values():
            v.pop("live_duration_seconds", None)
        return list(merged.values())

    # ===================== Image render: douchong table =====================
    @staticmethod
    def _render_table_image(
        title: str, data_list: List[Dict[str, Any]], period_display: str, query_source_text: str,
    ) -> str:
        total_live_duration_seconds = 0
        total_blind_box_count = 0
        total_blind_box_profit = 0.0
        total_gift = 0.0
        total_sc = 0.0
        total_guard = 0.0
        total_sum = 0.0

        for d in data_list:
            gift = _to_float(d.get("gift", 0))
            sc = _to_float(d.get("super_chat", 0))
            guard = _to_float(d.get("guard", 0))
            d["total"] = gift + sc + guard
            d["duration_fmt"] = format_duration(d.get("live_duration", "00:00:00"))
            d["fans_fmt"] = format_fans(_to_int(d.get("attention", 0)))
            total_live_duration_seconds += _duration_to_seconds(d.get("live_duration", "00:00:00"))
            total_blind_box_count += _to_int(d.get("blind_box_count", 0))
            total_blind_box_profit += _to_float(d.get("blind_box_profit", 0))
            total_gift += gift
            total_sc += sc
            total_guard += guard
            total_sum += d["total"]

        data_list.sort(key=lambda x: _to_float(x.get("total", 0)), reverse=True)

        row_height = 60
        col_widths = [
            300, 140, 150, 200, 180, 100,
            90, 90, 90, 120, 100, 130,
            180, 180, 180, 220,
        ]
        headers = [
            "主播名称", "粉丝数", "直播状态", "直播时间", "时薪", "有效天",
            "舰长", "提督", "总督", "粉丝团", "盲盒数", "盲盒盈亏",
            "礼物", "SC", "上舰", "总计",
        ]

        table_width = sum(col_widths) + 40
        table_height = row_height * (len(data_list) + 2) + 40
        canvas_width = table_width
        canvas_height = table_height + 190

        pic = PicGenerator(canvas_width, canvas_height)
        pic.set_pos(0, 0).draw_rounded_rectangle(0, 0, canvas_width, canvas_height, 0, Color.WHITE)

        LEFT_PADDING = 20
        TIME_Y = 90
        SOURCE_Y = 120
        MONTH_Y = 150
        TIP_X_OFFSET = 300

        pic.set_pos(LEFT_PADDING, 30).draw_text(title, [Color.BLACK])
        now_str = timestamp_format(int(time.time()), "%Y-%m-%d %H:%M:%S")
        pic.set_pos(LEFT_PADDING, TIME_Y).draw_text(now_str, [Color.GRAY])
        pic.set_pos(LEFT_PADDING + TIP_X_OFFSET, TIME_Y).draw_text(
            "数据每月号开始统计，月底清零。", [Color.GRAY]
        )
        pic.set_pos(LEFT_PADDING, SOURCE_Y).draw_text(query_source_text, [Color.GRAY])
        pic.set_pos(LEFT_PADDING, MONTH_Y).draw_text(f"统计周期：{period_display}", [Color.GRAY])

        origin_x = 20
        origin_y = 190
        cur_y = origin_y

        pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, Color.DEEPSKYBLUE)
        cur_x = origin_x + 10
        for w, h in zip(col_widths, headers):
            pic.set_pos(cur_x, cur_y + 18).draw_text(h, [Color.WHITE])
            cur_x += w
        cur_y += row_height

        for idx, d in enumerate(data_list):
            cur_x = origin_x + 10
            status_txt = "直播中" if _to_int(d.get("status", 0)) == 1 else "未开播"
            status_col = Color.DEEPSKYBLUE if _to_int(d.get("status", 0)) == 1 else Color.BLACK

            fields: List[Tuple[str, Any]] = [
                (str(d.get("anchor_name", "")), Color.BLACK),
                (str(d.get("fans_fmt", "0")), Color.BLACK),
                (status_txt, status_col),
                (str(d.get("duration_fmt", "")), Color.BLACK),
                (format_hourly_rate(d.get("total", 0), d.get("live_duration", "00:00:00")), Color.BLACK),
                (str(d.get("effective_days", "")), Color.BLACK),
                (format_count(d.get("guard_1")), Color.BLACK),
                (format_count(d.get("guard_2")), Color.BLACK),
                (format_count(d.get("guard_3")), Color.BLACK),
                (format_count(d.get("fans_count")), Color.BLACK),
                (format_count(d.get("blind_box_count")), Color.BLACK),
                (f"{_to_float(d.get('blind_box_profit', 0)):.1f}", Color.BLACK),
                (f"{_to_float(d.get('gift', 0)):.1f}", Color.BLACK),
                (f"{_to_float(d.get('super_chat', 0)):.1f}", Color.BLACK),
                (f"{_to_float(d.get('guard', 0)):.1f}", Color.BLACK),
                (f"{_to_float(d.get('total', 0)):.1f}", Color.BLACK),
            ]
            bg = Color.LIGHTGRAY if (idx % 2 == 0) else Color.WHITE
            pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, bg)
            for w, (text, txt_color) in zip(col_widths, fields):
                pic.set_pos(cur_x, cur_y + 18).draw_text(str(text), [txt_color])
                cur_x += w
            cur_y += row_height

        pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, Color.LIGHTGRAY)
        total_fields: List[Tuple[str, Any]] = [
            ("\u603b\u8ba1", Color.BLACK), ("", Color.BLACK), ("", Color.BLACK),
            (format_duration(_seconds_to_duration(total_live_duration_seconds)), Color.BLACK),
            (format_hourly_rate(total_sum, _seconds_to_duration(total_live_duration_seconds)), Color.BLACK),
            ("", Color.BLACK), ("", Color.BLACK), ("", Color.BLACK), ("", Color.BLACK), ("", Color.BLACK),
            (str(total_blind_box_count), Color.BLACK), (f"{total_blind_box_profit:.1f}", Color.BLACK),
            (f"{total_gift:.1f}", Color.BLACK), (f"{total_sc:.1f}", Color.BLACK),
            (f"{total_guard:.1f}", Color.BLACK), (f"{total_sum:.1f}", Color.BLACK),
        ]
        cur_x = origin_x + 10
        for w, (text, txt_color) in zip(col_widths, total_fields):
            pic.set_pos(cur_x, cur_y + 18).draw_text(str(text), [txt_color])
            cur_x += w

        pic.set_pos(canvas_width - 220, canvas_height - 40)
        pic.draw_text_right(0, "Designed by 开发猫", Color.GRAY)
        pic.crop_and_paste_bottom()
        return pic.base64()

    # ===================== Douchong handler =====================
    async def _handle_douchong(
        self, event: AstrMessageEvent, arg: str, *, api_base: str, title: str,
    ):
        period = normalize_period_arg(arg)
        if not period:
            yield event.plain_result("参数格式不正确，请使用 YYYY、YYYYMM 或 YYYY-MM，例如：2026、202509 或 2025-09")
            return
        month_codes, period_display = period
        query_source_text = build_query_source_text(event)
        logger.info(f"[{title}] period={','.join(month_codes)} user={event.unified_msg_origin}")
        try:
            data_list = await self._fetch_period_data(api_base, month_codes)
        except Exception as e:
            yield event.plain_result(f"请求数据失败：{e}")
            return
        if not data_list:
            yield event.plain_result(f"无数据：{period_display}")
            return
        if len(month_codes) == 1 or _is_current_year_period(month_codes):
            apply_live_duration_calc(data_list)
        try:
            b64 = self._render_table_image(title, data_list, period_display, query_source_text)
        except Exception as e:
            logger.exception("render_table_image failed")
            yield event.plain_result(f"生成图片失败：{e}")
            return
        yield self._send_image(event, b64)

    # ===================== Douchong brawl handler =====================
    async def _handle_douchong_brawl(self, event: AstrMessageEvent, arg: str):
        period = normalize_period_arg(arg)
        if not period:
            yield event.plain_result("参数格式不正确，请使用 YYYY、YYYYMM 或 YYYY-MM，例如：2026、202601 或 2026-01")
            return
        month_codes, period_display = period
        query_source_text = build_query_source_text(event)
        title = "VR+PSP Brawl"
        logger.info(f"[{title}] period={','.join(month_codes)} user={event.unified_msg_origin}")
        try:
            vr_list = await self._fetch_period_data(
                self.cfg.get("vr_gift_api_base", "https://vr.qianqiuzy.cn/gift"), month_codes
            )
        except Exception as e:
            yield event.plain_result(f"请求 VR 数据失败：{e}")
            return
        try:
            psp_list = await self._fetch_period_data(
                self.cfg.get("psp_gift_api_base", "https://psp.qianqiuzy.cn/gift"), month_codes
            )
        except Exception as e:
            yield event.plain_result(f"请求 PSP 数据失败：{e}")
            return
        data_list = [d for d in (vr_list + psp_list) if isinstance(d, dict)]
        if not data_list:
            yield event.plain_result(f"无数据：{period_display}")
            return
        if len(month_codes) == 1 or _is_current_year_period(month_codes):
            apply_live_duration_calc(data_list)
        try:
            b64 = self._render_table_image(title, data_list, period_display, query_source_text)
        except Exception as e:
            logger.exception("render_table_image failed")
            yield event.plain_result(f"生成图片失败：{e}")
            return
        yield self._send_image(event, b64)

    # ===================== Live list: render =====================
    @staticmethod
    def _calc_live_duration_hms(live_time_str: str) -> str:
        try:
            start_dt = datetime.datetime.strptime(live_time_str, "%Y-%m-%d %H:%M:%S")
            delta = datetime.datetime.now() - start_dt
            total_seconds = max(0, int(delta.total_seconds()))
            h = total_seconds // 3600
            m = (total_seconds % 3600) // 60
            s = total_seconds % 60
            return f"{h:02d}:{m:02d}:{s:02d}"
        except Exception:
            return "00:00:00"

    @staticmethod
    def _limit_text_by_px(pic: PicGenerator, text: str, max_px: int) -> str:
        text = _safe_str(text)
        if not text:
            return ""
        measure = getattr(pic, "_measure_with_fallback", None)
        if not callable(measure):
            return text
        if measure(text) <= max_px:
            return text
        suffix = "..."
        suf_w = measure(suffix)
        acc = []
        for ch in text:
            acc.append(ch)
            if measure("".join(acc)) + suf_w > max_px:
                acc.pop()
                break
        return "".join(acc) + suffix

    @staticmethod
    def _render_live_list_image(title: str, live_list: List[Dict[str, Any]], query_source_text: str) -> str:
        row_height = 60
        col_widths = [350, 300, 200, 150, 600]
        headers = ["开播时间", "主播名称", "已开播时长", "即时同接", "直播标题"]
        table_width = sum(col_widths) + 40
        table_height = row_height * (len(live_list) + 1) + 40
        canvas_width = table_width
        canvas_height = table_height + 170

        pic = PicGenerator(canvas_width, canvas_height)
        pic.set_pos(0, 0).draw_rounded_rectangle(0, 0, canvas_width, canvas_height, 0, Color.WHITE)

        LEFT_PADDING = 20
        pic.set_pos(LEFT_PADDING, 30).draw_text(title, [Color.BLACK])
        now_str = timestamp_format(int(time.time()), "%Y-%m-%d %H:%M:%S")
        pic.set_pos(LEFT_PADDING, 90).draw_text(now_str, [Color.GRAY])
        pic.set_pos(LEFT_PADDING + 300, 90).draw_text("仅列出当前正在直播的房间", [Color.GRAY])
        pic.set_pos(LEFT_PADDING, 120).draw_text(query_source_text, [Color.GRAY])

        origin_x = 20
        origin_y = 170
        cur_y = origin_y

        pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, Color.DEEPSKYBLUE)
        cur_x = origin_x + 10
        for w, h in zip(col_widths, headers):
            pic.set_pos(cur_x, cur_y + 18).draw_text(h, [Color.WHITE])
            cur_x += w
        cur_y += row_height

        for idx, d in enumerate(live_list):
            bg = Color.LIGHTGRAY if (idx % 2 == 0) else Color.WHITE
            pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, bg)
            live_time_val = _safe_str(d.get("live_time", "0000-00-00 00:00:00"))
            anchor_name = _safe_str(d.get("anchor_name", ""))
            duration_val = VRPSPDouchong._calc_live_duration_hms(live_time_val)
            current_cc = _to_int(d.get("current_concurrency", 0))
            live_title = VRPSPDouchong._limit_text_by_px(pic, _safe_str(d.get("title", "")), max_px=col_widths[4] - 20)
            cur_x = origin_x + 10
            pic.set_pos(cur_x, cur_y + 18).draw_text(live_time_val, [Color.BLACK])
            cur_x += col_widths[0]
            pic.set_pos(cur_x, cur_y + 18).draw_text(anchor_name, [Color.BLACK])
            cur_x += col_widths[1]
            pic.set_pos(cur_x, cur_y + 18).draw_text(duration_val, [Color.BLACK])
            cur_x += col_widths[2]
            pic.set_pos(cur_x, cur_y + 18).draw_text(str(current_cc), [Color.BLACK])
            cur_x += col_widths[3]
            pic.set_pos(cur_x, cur_y + 18).draw_text(live_title, [Color.BLACK])
            cur_y += row_height

        pic.set_pos(canvas_width - 220, canvas_height - 40)
        pic.draw_text_right(0, "Designed by 开发猫", Color.GRAY)
        pic.crop_and_paste_bottom()
        return pic.base64()

    # ===================== Live list handlers =====================
    async def _handle_live_list(self, event: AstrMessageEvent, *, api_url: str, title: str):
        query_source_text = build_query_source_text(event)
        logger.info(f"[{title}] user={event.unified_msg_origin}")
        try:
            data = await self._fetch_json(api_url)
            data_list = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else [])
            if not isinstance(data_list, list):
                data_list = []
            data_list = [d for d in data_list if isinstance(d, dict)]
        except Exception as e:
            yield event.plain_result(f"请求数据失败：{e}")
            return
        live_list = [d for d in data_list if _to_int(d.get("status", 0)) == 1]
        if not live_list:
            yield event.plain_result("当前没有主播正在直播。")
            return
        live_list.sort(key=lambda d: _safe_str(d.get("live_time", "")), reverse=True)
        try:
            b64 = self._render_live_list_image(title, live_list, query_source_text)
        except Exception as e:
            logger.exception("render_live_list_image failed")
            yield event.plain_result(f"生成图片失败：{e}")
            return
        yield self._send_image(event, b64)

    async def _handle_live_list_brawl(self, event: AstrMessageEvent):
        query_source_text = build_query_source_text(event)
        title = "VR+PSP Live"
        logger.info(f"[{title}] user={event.unified_msg_origin}")
        vr_base = self.cfg.get("vr_gift_api_base", "https://vr.qianqiuzy.cn/gift")
        psp_base = self.cfg.get("psp_gift_api_base", "https://psp.qianqiuzy.cn/gift")
        try:
            vr_data, psp_data = await asyncio.gather(
                self._fetch_json(vr_base), self._fetch_json(psp_base)
            )
        except Exception as e:
            yield event.plain_result(f"请求数据失败：{e}")
            return
        for source_list in [vr_data, psp_data]:
            if not isinstance(source_list, list):
                source_list = []
        all_list = [d for d in (list(vr_data) + list(psp_data)) if isinstance(d, dict)]
        live_list = [d for d in all_list if _to_int(d.get("status", 0)) == 1]
        if not live_list:
            yield event.plain_result("当前没有主播正在直播。")
            return
        live_list.sort(key=lambda d: _safe_str(d.get("live_time", "")), reverse=True)
        try:
            b64 = self._render_live_list_image(title, live_list, query_source_text)
        except Exception as e:
            logger.exception("render_live_list_image failed")
            yield event.plain_result(f"生成图片失败：{e}")
            return
        yield self._send_image(event, b64)

    # ===================== Query: parse args =====================
    @staticmethod
    def _parse_anchor_and_month(arg_str: str) -> Tuple[str, str]:
        raw = (arg_str or "").strip()
        if not raw:
            return "", current_month_code()
        parts = re.split(r"\s+", raw)
        maybe_month = normalize_month_arg(parts[-1]) if parts else None
        if maybe_month:
            month_code = maybe_month
            anchor_kw = " ".join(parts[:-1]).strip()
        else:
            month_code = current_month_code()
            anchor_kw = raw
        return anchor_kw, month_code

    # ===================== Query: locate room =====================
    async def _locate_room_by_anchor(self, anchor_kw: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        bases = [
            self.cfg.get("vr_gift_api_base", "https://vr.qianqiuzy.cn/gift"),
            self.cfg.get("psp_gift_api_base", "https://psp.qianqiuzy.cn/gift"),
        ]
        for base in bases:
            try:
                data = await self._fetch_json(base)
                lst = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
                if not isinstance(lst, list):
                    continue
                match = _match_anchor([x for x in lst if isinstance(x, dict)], anchor_kw)
                if match:
                    return base, match
            except Exception as e:
                logger.warning(f"_locate_room_by_anchor fetch {base} failed: {e}")
                continue
        return None, None

    # ===================== Query: live sessions =====================
    async def _query_live_sessions(self, *, base: str, room_id: str, month_code: str) -> List[Dict[str, Any]]:
        url = f"{base}/live_sessions?room_id={room_id}&month={month_code}"
        payload = await self._fetch_json(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
            return []
        return [x for x in payload["sessions"] if isinstance(x, dict)]

    @staticmethod
    def _render_live_sessions_image(
        *, anchor_name: str, room_id: str, month_code: str,
        sessions: List[Dict[str, Any]], query_source_text: str,
    ) -> str:
        now = datetime.datetime.now()
        rows: List[Dict[str, Any]] = []
        total_danmu = 0
        total_box = 0
        total_profit = 0.0
        total_gift = 0.0
        total_guard = 0.0
        total_sc = 0.0
        total_sum = 0.0
        total_seconds = 0

        for s in sessions:
            start_str = str(s.get("start_time") or "")
            end_str = str(s.get("end_time") or "")
            title = str(s.get("title") or "")
            try:
                danmu = int(s.get("danmaku_count") or 0)
            except Exception:
                danmu = 0
            try:
                avg_cc = int(round(float(s.get("avg_concurrency") or 0)))
            except Exception:
                avg_cc = 0
            try:
                max_cc = int(s.get("max_concurrency") or 0)
            except Exception:
                max_cc = 0
            blind_box_count = int(s.get("blind_box_count") or 0)
            blind_box_profit = float(s.get("blind_box_profit") or 0)
            gift = float(s.get("gift") or 0)
            guard = float(s.get("guard") or 0)
            sc = float(s.get("super_chat") or 0)
            subtotal = gift + guard + sc
            dt_start = _parse_dt(start_str) if start_str else None
            dt_end = _parse_dt(end_str) if end_str else None
            if dt_start:
                if dt_end:
                    dur_sec = int((dt_end - dt_start).total_seconds())
                    end_disp = end_str
                else:
                    dur_sec = int((now - dt_start).total_seconds())
                    end_disp = "直播中"
            else:
                dur_sec = 0
                end_disp = end_str or "-"
            rows.append({
                "start": start_str or "-", "end": end_disp,
                "duration": _sec_to_hms(dur_sec), "danmu": danmu,
                "avg_cc": avg_cc, "max_cc": max_cc, "title": title,
                "blind_box_count": blind_box_count, "blind_box_profit": blind_box_profit,
                "gift": gift, "guard": guard, "sc": sc, "sum": subtotal,
            })
            total_box += blind_box_count
            total_profit += blind_box_profit
            total_danmu += danmu
            total_gift += gift
            total_guard += guard
            total_sc += sc
            total_sum += subtotal
            total_seconds += max(0, dur_sec)

        col_widths = [350, 350, 200, 120, 150, 150, 600, 100, 120, 150, 150, 150, 200]
        headers = ["开播时间", "下播时间", "本场直播时间", "弹幕数", "平均同接", "最高同接", "本场直播标题",
                    "盲盒数", "盲盒盈亏", "礼物", "舰长", "SC", "总计"]
        row_height = 60
        table_width = sum(col_widths) + 40
        header_h = 190
        n_rows = max(1, len(rows))
        table_height = row_height * (n_rows + 2) + 40
        canvas_width = table_width
        canvas_height = header_h + table_height

        pic = PicGenerator(canvas_width, canvas_height)
        pic.set_pos(0, 0).draw_rounded_rectangle(0, 0, canvas_width, canvas_height, 0, Color.WHITE)

        LEFT = 20
        month_label = "本月" if month_code == current_month_code() else f"{month_code}"
        pic.set_pos(LEFT, 30).draw_text(f"{anchor_name}{month_label}直播情况", [Color.BLACK])
        pic.set_pos(LEFT, 90).draw_text(f"房间号：{room_id}", [Color.GRAY])
        pic.set_pos(LEFT, 120).draw_text(query_source_text, [Color.GRAY])
        pic.set_pos(LEFT, 150).draw_text(
            f"查询时间：{timestamp_format(int(time.time()), '%Y-%m-%d %H:%M:%S')}", [Color.GRAY]
        )

        origin_x = 20
        cur_y = header_h
        pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, Color.DEEPSKYBLUE)
        cur_x = origin_x + 10
        for w, h in zip(col_widths, headers):
            pic.set_pos(cur_x, cur_y + 18).draw_text(h, [Color.WHITE])
            cur_x += w
        cur_y += row_height

        if rows:
            for idx, r in enumerate(rows):
                bg = Color.LIGHTGRAY if (idx % 2 == 0) else Color.WHITE
                pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, bg)
                cells = [
                    r["start"], r["end"], r["duration"], str(r["danmu"]),
                    str(r["avg_cc"]), str(r["max_cc"]), r["title"],
                    str(r["blind_box_count"]), f"{r['blind_box_profit']:.1f}",
                    f"{r['gift']:.1f}", f"{r['guard']:.1f}", f"{r['sc']:.1f}", f"{r['sum']:.1f}",
                ]
                cur_x = origin_x + 10
                for w, txt in zip(col_widths, cells):
                    pic.set_pos(cur_x, cur_y + 18).draw_text(str(txt), [Color.BLACK])
                    cur_x += w
                cur_y += row_height
        else:
            pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, Color.WHITE)
            pic.set_pos(origin_x + 10, cur_y + 18).draw_text("（无记录）", [Color.BLACK])
            cur_y += row_height

        pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, Color.LIGHTGRAY)
        total_cells = [
            f"场次：{len(rows)}", "", _sec_to_hms(total_seconds), str(total_danmu),
            "", "", "", str(total_box), f"{total_profit:.1f}",
            f"{total_gift:.1f}", f"{total_guard:.1f}", f"{total_sc:.1f}", f"{total_sum:.1f}",
        ]
        cur_x = origin_x + 10
        for w, txt in zip(col_widths, total_cells):
            pic.set_pos(cur_x, cur_y + 18).draw_text(str(txt), [Color.BLACK])
            cur_x += w

        pic.set_pos(canvas_width - 220, canvas_height - 40)
        pic.draw_text_right(0, "Designed by 开发猫", Color.GRAY)
        pic.crop_and_paste_bottom()
        return pic.base64()

    async def _handle_query_live(self, event: AstrMessageEvent, arg: str):
        anchor_kw, month_code = self._parse_anchor_and_month(arg)
        if not anchor_kw:
            yield event.plain_result("请指定主播名称，例如：查直播 花礼 202603")
            return
        base, match = await self._locate_room_by_anchor(anchor_kw)
        if not base or not match:
            yield event.plain_result("未找到用户")
            return
        anchor_name = str(match.get("anchor_name") or anchor_kw)
        room_id = str(match.get("room_id") or "")
        if not room_id:
            yield event.plain_result("该用户缺少房间信息")
            return
        try:
            sessions = await self._query_live_sessions(base=base, room_id=room_id, month_code=month_code)
        except Exception as e:
            yield event.plain_result(f"未能获取直播场次：{e}")
            return
        try:
            b64 = self._render_live_sessions_image(
                anchor_name=anchor_name, room_id=room_id, month_code=month_code,
                sessions=sessions, query_source_text=build_query_source_text(event),
            )
        except Exception as e:
            yield event.plain_result(f"生成图片失败：{e}")
            return
        yield self._send_image(event, b64)

    # ===================== Query: attention (fans) =====================
    async def _query_attention_snapshots(self, *, base: str, room_id: str, month_code: str) -> List[Tuple[str, int]]:
        url = f"{base}/attention?room_id={room_id}&month={month_code}"
        payload = await self._fetch_json(url)
        if not isinstance(payload, dict):
            return []
        items = payload.get("attention")
        if not isinstance(items, list):
            return []
        rows: List[Tuple[str, int]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            for raw_date, raw_count in item.items():
                date_text = str(raw_date).strip()
                if not re.fullmatch(r"\d{8}", date_text):
                    continue
                try:
                    count = int(raw_count)
                except Exception:
                    continue
                rows.append((date_text, count))
        rows.sort(key=lambda row: row[0])
        return rows

    @staticmethod
    def _format_attention_date(date_code: str) -> str:
        if re.fullmatch(r"\d{8}", date_code):
            return f"{date_code[:4]}-{date_code[4:6]}-{date_code[6:]}"
        return date_code

    @staticmethod
    def _render_attention_image(
        *, anchor_name: str, room_id: str, month_code: str,
        attention_rows: List[Tuple[str, int]], query_source_text: str,
    ) -> str:
        col_widths = [300, 260]
        headers = ["日期", "粉丝数"]
        row_height = 60
        header_h = 190
        n_rows = max(1, len(attention_rows))
        table_width = sum(col_widths) + 40
        table_height = row_height * (n_rows + 2) + 40
        canvas_width = table_width
        canvas_height = header_h + table_height

        pic = PicGenerator(canvas_width, canvas_height)
        pic.set_pos(0, 0).draw_rounded_rectangle(0, 0, canvas_width, canvas_height, 0, Color.WHITE)

        LEFT = 20
        month_label = "本月" if month_code == current_month_code() else f"{month_code}"
        pic.set_pos(LEFT, 30).draw_text(f"{anchor_name}{month_label}粉丝情况", [Color.BLACK])
        pic.set_pos(LEFT, 90).draw_text(f"房间号：{room_id}", [Color.GRAY])
        pic.set_pos(LEFT, 120).draw_text(query_source_text, [Color.GRAY])
        pic.set_pos(LEFT, 150).draw_text(
            f"查询时间：{timestamp_format(int(time.time()), '%Y-%m-%d %H:%M:%S')}", [Color.GRAY]
        )

        origin_x = 20
        cur_y = header_h
        pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, Color.DEEPSKYBLUE)
        cur_x = origin_x + 10
        for w, h in zip(col_widths, headers):
            pic.set_pos(cur_x, cur_y + 18).draw_text(h, [Color.WHITE])
            cur_x += w
        cur_y += row_height

        if attention_rows:
            for idx, (date_code, count) in enumerate(attention_rows):
                bg = Color.LIGHTGRAY if (idx % 2 == 0) else Color.WHITE
                pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, bg)
                cells = [VRPSPDouchong._format_attention_date(date_code), str(count)]
                cur_x = origin_x + 10
                for w, txt in zip(col_widths, cells):
                    pic.set_pos(cur_x, cur_y + 18).draw_text(txt, [Color.BLACK])
                    cur_x += w
                cur_y += row_height
        else:
            pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, Color.WHITE)
            pic.set_pos(origin_x + 10, cur_y + 18).draw_text("（无记录）", [Color.BLACK])
            cur_y += row_height

        fans_delta = (attention_rows[-1][1] - attention_rows[0][1]) if len(attention_rows) >= 2 else 0
        pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_height, 0, Color.LIGHTGRAY)
        cur_x = origin_x + 10
        for w, txt in zip(col_widths, ["涨粉数", str(fans_delta)]):
            pic.set_pos(cur_x, cur_y + 18).draw_text(txt, [Color.BLACK])
            cur_x += w

        pic.set_pos(canvas_width - 220, canvas_height - 40)
        pic.draw_text_right(0, "Designed by 开发猫", Color.GRAY)
        pic.crop_and_paste_bottom()
        return pic.base64()

    async def _handle_query_attention(self, event: AstrMessageEvent, arg: str):
        anchor_kw, month_code = self._parse_anchor_and_month(arg)
        if not anchor_kw:
            yield event.plain_result("请指定主播名称，例如：查粉丝 花礼 202603")
            return
        base, match = await self._locate_room_by_anchor(anchor_kw)
        if not base or not match:
            yield event.plain_result("未找到用户")
            return
        anchor_name = str(match.get("anchor_name") or anchor_kw)
        room_id = str(match.get("room_id") or "")
        if not room_id:
            yield event.plain_result("该用户缺少房间信息")
            return
        try:
            attention_rows = await self._query_attention_snapshots(base=base, room_id=room_id, month_code=month_code)
        except Exception as e:
            yield event.plain_result(f"未能获取粉丝数据：{e}")
            return
        try:
            b64 = self._render_attention_image(
                anchor_name=anchor_name, room_id=room_id, month_code=month_code,
                attention_rows=attention_rows, query_source_text=build_query_source_text(event),
            )
        except Exception as e:
            yield event.plain_result(f"生成图片失败：{e}")
            return
        yield self._send_image(event, b64)

    # ===================== Query: SC =====================
    async def _query_sc_list(self, *, base: str, room_id: str, month_code: str) -> List[Dict[str, Any]]:
        url = f"{base}/sc?room_id={room_id}&month={month_code}"
        payload = await self._fetch_json(url)
        if isinstance(payload, dict):
            lst = payload.get("list")
            if isinstance(lst, list):
                return [x for x in lst if isinstance(x, dict)]
            return []
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        return []

    @staticmethod
    def _clean_sc_message(raw: str) -> str:
        if not raw:
            return ""
        s = str(raw).replace("\n", " ").replace("\r", " ")
        s = re.sub(r"\s+", " ", s)
        return s.strip()

    @staticmethod
    def _wrap_sc_message(text: str, max_chars: int) -> List[str]:
        if not text:
            return [""]
        lines: List[str] = []
        i = 0
        while i < len(text):
            lines.append(text[i : i + max_chars])
            i += max_chars
        return lines or [""]

    @staticmethod
    def _limit_uname_visual(uname: str, max_units: float = 12.0) -> str:
        uname = uname or ""
        units = 0.0
        kept: List[str] = []
        for ch in uname:
            w = 0.5 if ord(ch) < 128 else 1.0
            if units + w > max_units:
                break
            kept.append(ch)
            units += w
        if len(kept) == len(uname):
            return uname
        ellipsis_w = 1.0
        while kept and units + ellipsis_w > max_units:
            last = kept.pop()
            units -= (0.5 if ord(last) < 128 else 1.0)
        kept.append("...")
        return "".join(kept)

    @staticmethod
    def _safe_dt(s: str) -> datetime.datetime:
        try:
            return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.datetime.min

    SC_MAX_PAGE_HEIGHT = 16000
    BASE_ROW_H = 60
    EXTRA_PER_LINE = 28
    MSG_MAX_CHARS_PER_LINE = 20

    @staticmethod
    def _paginate_rows_by_height(
        rows: List[Dict[str, Any]], *, max_canvas_h: int,
        header_h: int, table_header_h: int, table_padding_h: int = 40,
    ) -> List[Tuple[int, List[Dict[str, Any]]]]:
        max_data_h = max_canvas_h - header_h - table_header_h - table_padding_h
        if max_data_h <= 0:
            max_data_h = 1
        pages: List[Tuple[int, List[Dict[str, Any]]]] = []
        cur: List[Dict[str, Any]] = []
        cur_h = 0
        start_idx = 0
        global_idx = 0
        for r in rows:
            rh = int(r.get("row_height") or 0)
            if cur and (cur_h + rh > max_data_h):
                pages.append((start_idx, cur))
                cur = []
                cur_h = 0
                start_idx = global_idx
            cur.append(r)
            cur_h += rh
            global_idx += 1
        if cur:
            pages.append((start_idx, cur))
        return pages or [(0, [])]

    @staticmethod
    def _render_sc_page(
        anchor_name: str, room_id: str, month_code: str,
        page_rows: List[Dict[str, Any]], total_rows: int,
        page_no: int, total_pages: int, global_start_idx: int,
        query_source_text: str,
    ) -> str:
        col_widths = [350, 350, 300, 100, 700]
        headers = ["发送时间", "发送人", "UID", "价格", "内容"]
        header_h = 190
        table_header_h = VRPSPDouchong.BASE_ROW_H
        data_height = sum(int(r["row_height"]) for r in page_rows) if page_rows else VRPSPDouchong.BASE_ROW_H
        table_width = sum(col_widths) + 40
        table_height = table_header_h + data_height + 40
        canvas_width = table_width
        canvas_height = header_h + table_height

        pic = PicGenerator(canvas_width, canvas_height)
        pic.set_pos(0, 0).draw_rounded_rectangle(0, 0, canvas_width, canvas_height, 0, Color.WHITE)

        LEFT = 20
        month_disp = f"{month_code[:4]}-{month_code[4:]}" if len(month_code) == 6 else month_code
        title_text = f"{anchor_name} {month_disp} SC ({page_no}/{total_pages})"
        pic.set_pos(LEFT, 30).draw_text(title_text, [Color.BLACK])
        pic.set_pos(LEFT, 90).draw_text(f"房间号：{room_id}", [Color.GRAY])
        pic.set_pos(LEFT, 120).draw_text(query_source_text, [Color.GRAY])
        now_str = timestamp_format(int(time.time()), "%Y-%m-%d %H:%M:%S")
        count_str = f"共 {total_rows} 条" if total_rows else "暂无记录"
        pic.set_pos(LEFT, 150).draw_text(f"查询时间：{now_str}  |  {count_str}", [Color.GRAY])

        origin_x = 20
        cur_y = header_h
        pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, table_header_h, 0, Color.DEEPSKYBLUE)
        cur_x = origin_x + 10
        for w, h in zip(col_widths, headers):
            pic.set_pos(cur_x, cur_y + 18).draw_text(h, [Color.WHITE])
            cur_x += w
        cur_y += table_header_h

        if page_rows:
            for idx, r in enumerate(page_rows):
                row_h = int(r["row_height"])
                bg = Color.LIGHTGRAY if ((global_start_idx + idx) % 2 == 0) else Color.WHITE
                pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, row_h, 0, bg)
                cur_x = origin_x + 10
                base_y = cur_y + 18
                cells = [r["time"], r["uname"], r["uid"], r["price"]]
                for w, txt in zip(col_widths[:4], cells):
                    pic.set_pos(cur_x, base_y).draw_text(str(txt), [Color.BLACK])
                    cur_x += w
                msg_x = cur_x
                y = base_y
                for line in r["msg_lines"]:
                    pic.set_pos(msg_x, y).draw_text(line, [Color.BLACK])
                    y += VRPSPDouchong.EXTRA_PER_LINE
                cur_y += row_h
        else:
            pic.draw_rounded_rectangle(origin_x, cur_y, table_width - 40, VRPSPDouchong.BASE_ROW_H, 0, Color.WHITE)
            pic.set_pos(origin_x + 10, cur_y + 18).draw_text("（本月暂无 SC 记录）", [Color.BLACK])

        pic.set_pos(canvas_width - 220, canvas_height - 40)
        pic.draw_text_right(0, "Designed by 开发猫", Color.GRAY)
        pic.crop_and_paste_bottom()
        return pic.base64()

    async def _handle_query_sc(self, event: AstrMessageEvent, arg: str):
        anchor_kw, month_code = self._parse_anchor_and_month(arg)
        if not anchor_kw:
            yield event.plain_result("请指定主播名称，例如：查SC 花礼 202603")
            return
        base, match = await self._locate_room_by_anchor(anchor_kw)
        if not base or not match:
            yield event.plain_result("未找到用户")
            return
        anchor_name = str(match.get("anchor_name") or anchor_kw)
        room_id = str(match.get("room_id") or "")
        if not room_id:
            yield event.plain_result("该用户缺少房间信息")
            return
        try:
            sc_list = await self._query_sc_list(base=base, room_id=room_id, month_code=month_code)
        except Exception as e:
            yield event.plain_result(f"未能获取 SC 记录：{e}")
            return

        sc_sorted = sorted(sc_list, key=lambda it: self._safe_dt(str(it.get("send_time", ""))))
        rows = []
        for item in sc_sorted:
            send_time = str(item.get("send_time", "") or "")
            uname_raw = str(item.get("uname", "") or "")
            uname = self._limit_uname_visual(uname_raw, max_units=12.0)
            uid = str(item.get("uid", "") or "")[:16]
            try:
                price_val = float(item.get("price") or 0)
            except Exception:
                price_val = 0.0
            price_str = str(int(round(price_val)))[:5]
            msg_clean = self._clean_sc_message(str(item.get("message", "") or ""))
            msg_lines = self._wrap_sc_message(msg_clean, self.MSG_MAX_CHARS_PER_LINE)
            line_count = max(1, len(msg_lines))
            row_h = self.BASE_ROW_H + (line_count - 1) * self.EXTRA_PER_LINE
            rows.append({
                "time": send_time, "uname": uname, "uid": uid,
                "price": price_str, "msg_lines": msg_lines, "row_height": row_h,
            })

        pages = self._paginate_rows_by_height(
            rows, max_canvas_h=self.SC_MAX_PAGE_HEIGHT,
            header_h=190, table_header_h=self.BASE_ROW_H, table_padding_h=40,
        )
        total_pages = len(pages)
        query_source_text = build_query_source_text(event)

        # Collect all page images as temp files
        node_list = []
        for page_no, (global_start_idx, page_rows) in enumerate(pages, start=1):
            b64 = self._render_sc_page(
                anchor_name=anchor_name, room_id=room_id, month_code=month_code,
                page_rows=page_rows, total_rows=len(rows),
                page_no=page_no, total_pages=total_pages,
                global_start_idx=global_start_idx,
                query_source_text=query_source_text,
            )
            img_bytes = base64.b64decode(b64)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(img_bytes)
                tmp_path = f.name
            node_list.append(Node(
                uin=event.get_self_id() or 10000,
                name=f"SC {page_no}/{total_pages}",
                content=[Image.fromFileSystem(tmp_path)]
            ))
        yield event.chain_result([Nodes(nodes=node_list)])

    # ===================== Query: income =====================
    async def _handle_query_financial(self, event: AstrMessageEvent, arg: str):
        anchor_kw, month_code = self._parse_anchor_and_month(arg)
        if not anchor_kw:
            yield event.plain_result("请指定主播名称，例如：查流水 花礼 202603")
            return
        base, match = await self._locate_room_by_anchor(anchor_kw)
        if not base or not match:
            yield event.plain_result("未找到用户")
            return
        anchor_name = str(match.get("anchor_name") or anchor_kw)
        room_id = str(match.get("room_id") or "")
        if not room_id:
            yield event.plain_result("该用户缺少房间信息")
            return

        vr_base = self.cfg.get("vr_gift_api_base", "https://vr.qianqiuzy.cn/gift")
        psp_base = self.cfg.get("psp_gift_api_base", "https://psp.qianqiuzy.cn/gift")

        all_data = []
        try:
            vr_raw = await self._fetch_json(f"{vr_base}?month={month_code}")
            if isinstance(vr_raw, list):
                all_data.extend([d for d in vr_raw if isinstance(d, dict) and str(d.get("room_id", "")) == room_id])
        except Exception:
            pass
        try:
            psp_raw = await self._fetch_json(f"{psp_base}?month={month_code}")
            if isinstance(psp_raw, list):
                all_data.extend([d for d in psp_raw if isinstance(d, dict) and str(d.get("room_id", "")) == room_id])
        except Exception:
            pass

        if not all_data:
            yield event.plain_result(f"未找到 {anchor_name} 在 {month_code} 的数据")
            return

        query_source_text = build_query_source_text(event)
        try:
            b64 = self._render_table_image(
                f"{anchor_name}流水", all_data,
                f"{month_code[:4]}-{month_code[4:]}", query_source_text,
            )
        except Exception as e:
            logger.exception("render_table_image failed")
            yield event.plain_result(f"生成图片失败：{e}")
            return
        yield self._send_image(event, b64)

    # ===================== Image send helper =====================
    @staticmethod
    def _send_image(event: AstrMessageEvent, b64: str):
        """Decode base64 image, save to temp file, send via AstrBot."""
        img_bytes = base64.b64decode(b64)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(img_bytes)
            tmp_path = f.name
        return event.image_result(tmp_path)
