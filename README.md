# astrbot_plugin_vrpspdouchong

VR/PSP 礼物统计与斗虫查询插件（AstrBot 移植版）

> 从 [nonebot-plugin-vrpspdouchong](https://github.com/QianQiuZy/nonebot-plugin-vrpspdouchong) 移植至 AstrBot 平台。

## 功能

| 命令 | 别名 | 说明 |
|---|---|---|
| `VR斗虫` | `vr斗虫` | VR 礼物月度统计榜单 |
| `PSP斗虫` | `psp斗虫` | PSP 礼物月度统计榜单 |
| `大乱斗斗虫` | - | VR + PSP 合并榜单 |
| `VR开播` | `vr开播` | VR 当前直播列表 |
| `PSP开播` | `psp开播` | PSP 当前直播列表 |
| `大乱斗开播` | - | VR + PSP 合并直播列表 |
| `查直播` | - | 查询主播某月直播场次明细 |
| `查SC` | - | 查询主播某月 SC 记录 |
| `查粉丝` | - | 查询主播某月每日粉丝数快照 |
| `查流水` | - | 查询主播某月流水概览 |

### 斗虫榜单

指令格式：

```
VR斗虫 [YYYYMM|YYYY-MM|YYYY]
PSP斗虫 [YYYYMM|YYYY-MM|YYYY]
大乱斗斗虫 [YYYYMM|YYYY-MM|YYYY]
```

- `YYYYMM` — 指定月（如 `202603`）
- `YYYY-MM` — 指定月（如 `2026-03`）
- `YYYY` — 年累计（历史年统计 1-12 月，当年统计 1-当前月）
- 留空 — 当前月

### 开播列表

指令格式：

```
VR开播
PSP开播
大乱斗开播
```

返回当前正在直播的房间列表。

### 查询类

```
查直播 主播名称 [YYYYMM|YYYY-MM]
查SC 主播名称 [YYYYMM|YYYY-MM]
查粉丝 主播名称 [YYYYMM|YYYY-MM]
查流水 主播名称 [YYYYMM|YYYY-MM]
```

## 安装

1. 将插件放入 `AstrBot/data/plugins/astrbot_plugin_vrpspdouchong/`
2. 确保有字体文件在 `resource/` 目录下
3. 重启 AstrBot

### 依赖

- `httpx` — HTTP 请求
- `Pillow` — 图片渲染
- `fonttools` — 字体回退链
- `regex`（可选）— 复杂 emoji / 字素切分

## 配置

可在 AstrBot WebUI 中调整：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `vr_gift_api_base` | string | `https://vr.qianqiuzy.cn/gift` | VR 礼物统计 API 基址 |
| `psp_gift_api_base` | string | `https://psp.qianqiuzy.cn/gift` | PSP 礼物统计 API 基址 |
| `vr_douchong_title` | string | `VR斗虫` | VR 斗虫榜单图片标题 |
| `psp_douchong_title` | string | `PSP斗虫` | PSP 斗虫榜单图片标题 |
| `vr_live_title` | string | `VR开播` | VR 开播图片标题 |
| `psp_live_title` | string | `PSP开播` | PSP 开播图片标题 |
| `vr_http_timeout` | float | `5.0` | HTTP 请求超时（秒） |

## 字体

插件依赖以下字体文件（位于 `resource/` 目录）：

- `NotoSansSC.ttf`
- `NotoSans.ttf`
- `NotoEmoji.ttf`
- `NotoSansSymbols2.ttf`
- `NotoSansYi.ttf`
- `NotoSerifTibetan.ttf`

## 许可证

MIT License

原插件作者：QianQiuZy
