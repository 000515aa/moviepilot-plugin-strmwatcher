from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.scraping import ScrapingChain
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import MetaInfoPath, resolve_media_identity
from app.sdk.utilities import SystemUtils
from app.plugins import _PluginBase
from app.schemas import MediaType


class StrmWatcher(_PluginBase):
    """STRM 实时刮削：增量监控 strm 文件，自动补齐 NFO/高清海报并刷新 Emby；支持图片体检修复与全库扫描。"""

    plugin_name = "STRM实时刮削"
    plugin_desc = "增量监控 strm 目录，新文件落盘后自动识别、刮削元数据与高清图片，修复缺失/损坏海报，触发 Emby 单条刷新。"
    plugin_icon = "scraper.png"
    plugin_version = "1.1.0"
    plugin_label = "媒体库"
    plugin_author = "000515aa"
    plugin_config_prefix = "strmwatcher_"
    plugin_order = 8
    auth_level = 1

    # 配置项
    _enabled = False
    _onlyonce = False
    _backfill = False
    _cron = "*/2 * * * *"
    _watch_paths = ""
    _exclude_paths = ""
    _exts = ".strm"
    _settle_seconds = 30
    _mode = ""
    _max_per_run = 50
    _refresh_emby = False
    _emby_url = ""
    _emby_api_key = ""
    _path_replace = ""
    _fix_images = True
    _min_image_kb = 20
    _image_domains = "image.tmdb.org,theimage.tmdb.org,tmdb.img.cafe"
    _sweep_once = False

    _scheduler = None
    _event = Event()
    _lock = Lock()

    # 电影/剧集目录必备图片（Emby 主视觉）
    _MOVIE_IMAGES = ["poster.jpg", "fanart.jpg", "backdrop.jpg"]
    _SHOW_IMAGES = ["poster.jpg", "fanart.jpg"]

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并启动定时服务。"""
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._backfill = bool(config.get("backfill"))
        self._cron = config.get("cron") or "*/2 * * * *"
        self._watch_paths = config.get("watch_paths") or ""
        self._exclude_paths = config.get("exclude_paths") or ""
        self._exts = config.get("exts") or ".strm"
        self._settle_seconds = int(config.get("settle_seconds") or 30)
        self._mode = config.get("mode") or ""
        self._max_per_run = int(config.get("max_per_run") or 50)
        self._refresh_emby = bool(config.get("refresh_emby"))
        self._emby_url = (config.get("emby_url") or "").rstrip("/")
        self._emby_api_key = config.get("emby_api_key") or ""
        self._path_replace = config.get("path_replace") or ""
        self._fix_images = bool(config.get("fix_images", True))
        self._min_image_kb = int(config.get("min_image_kb") or 20)
        self._image_domains = config.get("image_domains") or self._image_domains
        self._sweep_once = bool(config.get("sweep_once"))

        if self._enabled and (self._onlyonce or self._sweep_once):
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._onlyonce:
                self._scheduler.add_job(
                    func=self.__scan,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="STRM实时刮削-立即运行一次",
                )
                self._onlyonce = False
            if self._sweep_once:
                self._scheduler.add_job(
                    func=self.__build_sweep_queue,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=10),
                    name="STRM实时刮削-全库图片体检",
                )
                self._sweep_once = False
            self.update_config(self._current_config())
            self._scheduler.start()

    def _current_config(self) -> dict:
        """返回当前内存配置字典，用于回写持久化。"""
        return {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "backfill": self._backfill,
            "cron": self._cron,
            "watch_paths": self._watch_paths,
            "exclude_paths": self._exclude_paths,
            "exts": self._exts,
            "settle_seconds": self._settle_seconds,
            "mode": self._mode,
            "max_per_run": self._max_per_run,
            "refresh_emby": self._refresh_emby,
            "emby_url": self._emby_url,
            "emby_api_key": self._emby_api_key,
            "path_replace": self._path_replace,
            "fix_images": self._fix_images,
            "min_image_kb": self._min_image_kb,
            "image_domains": self._image_domains,
            "sweep_once": self._sweep_once,
        }

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """不注册额外 API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """注册高频增量扫描服务。"""
        if not self._enabled:
            return []
        return [{
            "id": "StrmWatcher",
            "name": "STRM实时刮削",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.__scan,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "backfill", "label": "首轮处理存量"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "cron", "label": "扫描周期", "placeholder": "*/2 * * * *"}}]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "watch_paths", "label": "监控目录", "rows": 4,
                                    "placeholder": "每行一个目录，可加 #电影 / #电视剧 强制类型"}}]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "exclude_paths", "label": "排除目录", "rows": 2, "placeholder": "每行一个目录"}}]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSelect", "props": {
                                    "model": "mode", "label": "覆盖模式",
                                    "items": [
                                        {"title": "已有NFO则跳过", "value": ""},
                                        {"title": "覆盖全部元数据", "value": "force_all"},
                                    ]}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "settle_seconds", "label": "文件静默等待(秒)", "type": "number"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "max_per_run", "label": "单次最多处理", "type": "number"}}]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "fix_images", "label": "体检修复缺失/损坏图片"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "min_image_kb", "label": "图片最小KB(低于视为损坏)", "type": "number"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "sweep_once", "label": "立即全库图片体检(一次性)"}}]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "image_domains", "label": "TMDB图片域名回退链",
                                    "placeholder": "image.tmdb.org,theimage.tmdb.org,tmdb.img.cafe"}}]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "refresh_emby", "label": "刮削后刷新Emby"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "emby_url", "label": "Emby地址", "placeholder": "http://172.17.0.1:8097"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "emby_api_key", "label": "Emby API Key", "type": "password"}}]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "path_replace", "label": "路径映射", "rows": 2,
                                    "placeholder": "每行一条：本地前缀#Emby前缀"}}]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VAlert", "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": "首次运行只登记存量不刮削；开启“体检修复”后，即使NFO已存在也会检查 poster/fanart/backdrop 是否缺失或过小损坏：先走宿主刮削管道（TMDB+Fanart+豆瓣合并源），仍缺的按“域名回退链”直抓 TMDB original 高清原图；“全库图片体检”一次性排队扫描所有目录，每轮修复5个逐步完成。"}}]},
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "backfill": False,
            "cron": "*/2 * * * *",
            "watch_paths": "",
            "exclude_paths": "",
            "exts": ".strm",
            "settle_seconds": 30,
            "mode": "",
            "max_per_run": 50,
            "refresh_emby": False,
            "emby_url": "",
            "emby_api_key": "",
            "path_replace": "",
            "fix_images": True,
            "min_image_kb": 20,
            "image_domains": "image.tmdb.org,theimage.tmdb.org,tmdb.img.cafe",
            "sweep_once": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回最近处理历史页面。"""
        history = self.get_data("history") or []
        stats = self.get_data("stats") or {}
        queue = self.get_data("sweep_queue") or []
        rows = [
            {
                "path": h.get("path", ""),
                "ident": h.get("ident", ""),
                "status": h.get("status", ""),
                "time": h.get("ts", ""),
                "error": h.get("error", ""),
            }
            for h in history[-50:]
        ]
        rows.reverse()
        summary = (f"累计成功 {stats.get('success', 0)} ｜ 失败 {stats.get('fail', 0)} ｜ "
                   f"跳过 {stats.get('skip', 0)} ｜ 图片修复 {stats.get('imgfix', 0)} ｜ "
                   f"全库体检待处理 {len(queue)} 个目录")
        return [
            {
                "component": "VCard",
                "props": {"title": "STRM实时刮削"},
                "content": [
                    {"component": "VCardText", "content": [
                        {"component": "VChip", "props": {"label": True, "color": "primary"}, "text": summary},
                        {"component": "VDataTable", "props": {
                            "headers": [
                                {"title": "文件", "key": "path"},
                                {"title": "识别", "key": "ident"},
                                {"title": "状态", "key": "status"},
                                {"title": "时间", "key": "time"},
                                {"title": "错误", "key": "error"},
                            ],
                            "items": rows,
                            "density": "compact",
                            "hover": True,
                        }},
                    ]},
                ]
            }
        ]

    # ==================== 核心逻辑 ====================

    def __scan(self) -> None:
        """执行一轮增量扫描（含体检队列消费）。"""
        if not self._lock.acquire(blocking=False):
            logger.info("STRM刮削：上一轮扫描尚未结束，跳过本轮")
            return
        try:
            self.__do_scan()
            self.__drain_sweep_queue()
        except Exception as err:
            logger.error(f"STRM刮削：扫描异常：{str(err)}")
        finally:
            self._lock.release()

    def __do_scan(self) -> None:
        """扫描主体：发现新文件、去抖、入队处理。"""
        if not self._watch_paths:
            return
        seen: Dict[str, dict] = self.get_data("seen") or {}
        baseline = not self.get_data("baseline_done") and not self._backfill
        now = datetime.now().timestamp()
        exts = [e.strip() for e in self._exts.split(",") if e.strip()]
        excludes = [p for p in self._exclude_paths.splitlines() if p.strip()]
        candidates: List[Tuple[Path, Optional[MediaType]]] = []
        for raw in self._watch_paths.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            forced = None
            if raw.count("#") == 1:
                raw, tag = raw.split("#")
                forced = MediaType.TV if "剧" in tag else MediaType.MOVIE if "影" in tag else None
            root = Path(raw)
            if not root.exists():
                logger.warning(f"STRM刮削：监控目录不存在 {raw}")
                continue
            for file_path in SystemUtils.list_files(root, exts):
                if self._event.is_set():
                    return
                if self.__is_excluded(file_path, excludes):
                    continue
                key = str(file_path)
                try:
                    stat = file_path.stat()
                except OSError:
                    continue
                record = seen.get(key)
                if record and record.get("m") == stat.st_mtime and record.get("s") == stat.st_size:
                    continue
                if now - stat.st_mtime < self._settle_seconds:
                    continue
                if record and record.get("f", 0) >= 5:
                    continue
                if len(candidates) >= (5000 if baseline else self._max_per_run):
                    break
                candidates.append((file_path, forced))
        if not candidates:
            logger.debug("STRM刮削：无新增文件")
            return
        if baseline:
            for file_path, _ in candidates:
                self.__mark_seen(seen, file_path, done=True)
            self.save_data("seen", seen)
            if len(candidates) < 5000:
                # 存量登记完毕；未扫完时下轮继续登记而不是刮削
                self.save_data("baseline_done", True)
            logger.info(f"STRM刮削：首轮登记存量文件 {len(candidates)} 个（未刮削，可在配置中开启存量处理）")
            return
        logger.info(f"STRM刮削：本轮发现 {len(candidates)} 个新文件")
        for file_path, forced in candidates:
            if self._event.is_set():
                return
            self.__process_file(file_path, forced, seen)
        self.save_data("seen", seen)

    def __process_file(self, file_path: Path, forced: Optional[MediaType], seen: Dict[str, dict]) -> None:
        """处理单个新 strm 文件：识别 → 刮削/图片修复 → 刷新。"""
        key = str(file_path)
        ident, status, error = "", "failed", ""
        try:
            mediainfo, mtype, ident = self.__recognize(file_path, forced)
            if mediainfo is None:
                self.__mark_seen(seen, file_path, done=True)
                self.__bump("skip")
                logger.info(f"STRM刮削：跳过 {key}（{ident}）")
                return
            if self.__nfo_exists(file_path, mtype) and self._mode != "force_all":
                # 已有 NFO：按需只做图片体检修复
                fixed = False
                if self._fix_images:
                    fixed = self.__repair_images(file_path, mtype, mediainfo)
                    if fixed:
                        self.__bump("imgfix")
                        self.__emby_refresh_after(file_path, mtype, mediainfo)
                self.__mark_seen(seen, file_path, done=True)
                self.__bump("skip")
                logger.info(f"STRM刮削：已存在NFO，{'图片已修复' if fixed else '图片完好'} {key}")
                return
            self.chain.obtain_images(mediainfo)
            item_path = str(file_path).replace("\\", "/")
            target_type = "file"
            if mtype == MediaType.MOVIE:
                item_path = f"{file_path.parent}/".replace("\\", "/")
                target_type = "dir"
            ScrapingChain().scrape_metadata(
                fileitem=schemas.FileItem(
                    storage="local",
                    type=target_type,
                    path=item_path,
                    name=(file_path.parent if target_type == "dir" else file_path).name,
                    basename=(file_path.parent if target_type == "dir" else file_path).stem,
                    extension=None if target_type == "dir" else file_path.suffix[1:],
                    modify_time=file_path.stat().st_mtime,
                ),
                mediainfo=mediainfo,
                overwrite=True if self._mode == "force_all" else False,
            )
            # 刮削后仍缺失/损坏的图片走回退域名补抓高清原图
            if self._fix_images:
                self.__repair_images(file_path, mtype, mediainfo)
            status = "success"
            self.__mark_seen(seen, file_path, done=True)
            self.__bump("success")
            self.__emby_refresh_after(file_path, mtype, mediainfo)
            logger.info(f"STRM刮削：完成 {key} → {ident}")
        except Exception as err:
            status = "failed"
            error = str(err)[:200]
            self.__mark_seen(seen, file_path, done=False)
            self.__bump("fail")
            logger.warning(f"STRM刮削：失败 {key}：{error}")
        finally:
            self.__append_history({"path": key, "ident": ident, "status": status,
                                   "error": error, "ts": datetime.now().strftime("%m-%d %H:%M:%S")})

    def __recognize(self, file_path: Path, forced: Optional[MediaType]) -> Tuple[Any, MediaType, str]:
        """识别 strm 文件对应的媒体信息；返回 (mediainfo或None, 类型, 说明)。"""
        meta = MetaInfoPath(file_path)
        mtype = forced or meta.type
        if mtype == MediaType.UNKNOWN:
            mtype = MediaType.MOVIE
        media_source, media_id = resolve_media_identity(
            media_source=meta.media_source, media_id=meta.media_id)
        if media_source and media_id:
            mediainfo = self.chain.recognize_media(
                media_source=media_source, media_id=str(media_id), mtype=mtype)
        else:
            meta.type = mtype
            mediainfo = self.chain.recognize_media(meta=meta)
        if not mediainfo:
            return None, mtype, "未识别到媒体信息"
        if not mediainfo.tmdb_id:
            # MP v3 核心 obtain_images 对无 tmdb_id 的身份会抛断言错误，主动跳过
            return None, mtype, f"识别结果无TMDB ID（{mediainfo.media_source.value}:{mediainfo.media_id}）"
        # 宿主 NFO 模块要求 scrape_source 与识别源精确匹配；
        # SCRAP_SOURCE 为多源链时不会命中，这里显式锁定本次识别来源。
        if mediainfo.media_source:
            mediainfo.scrape_source = str(mediainfo.media_source.value)
        return mediainfo, mtype, f"{mediainfo.media_source.value}:{mediainfo.media_id} {mediainfo.title}"

    # ==================== 图片体检与修复 ====================

    def __media_target_dir(self, file_path: Path, mtype: MediaType) -> Path:
        """计算图片应存放的媒体目录。"""
        if mtype == MediaType.MOVIE:
            return file_path.parent
        # 剧集：季目录的上一级为剧目录
        parent = file_path.parent
        if parent.name.lower().startswith("season") or parent.name.startswith("第"):
            return parent.parent
        return parent

    def __expected_images(self, file_path: Path, mtype: MediaType) -> List[Path]:
        """返回该媒体应必备的图片文件列表。"""
        if mtype == MediaType.MOVIE:
            return [file_path.parent / name for name in self._MOVIE_IMAGES]
        show_dir = self.__media_target_dir(file_path, mtype)
        return [show_dir / name for name in self._SHOW_IMAGES]

    def __bad_images(self, paths: List[Path]) -> Tuple[List[Path], List[Path]]:
        """区分缺失与损坏（小于阈值）的图片文件。"""
        min_bytes = max(int(self._min_image_kb), 1) * 1024
        missing, broken = [], []
        for p in paths:
            if not p.exists():
                missing.append(p)
            elif p.stat().st_size < min_bytes:
                broken.append(p)
        return missing, broken

    def __repair_images(self, file_path: Path, mtype: MediaType, mediainfo: Any) -> bool:
        """体检并修复媒体图片：删除损坏文件，先走宿主刮削管道，再走 TMDB 域名回退补抓。"""
        expected = self.__expected_images(file_path, mtype)
        missing, broken = self.__bad_images(expected)
        if not missing and not broken:
            return False
        for p in broken:
            try:
                p.unlink()
            except OSError:
                pass
        target_dir = file_path.parent if mtype == MediaType.MOVIE else self.__media_target_dir(file_path, mtype)
        logger.info(f"STRM刮削：图片体检 {target_dir} 缺失{len(missing)}损坏{len(broken)}，尝试修复")
        # 第一轮：宿主管道（TMDB+Fanart+豆瓣 合并源）
        try:
            self.chain.obtain_images(mediainfo)
            item_path = f"{target_dir}/".replace("\\", "/")
            ScrapingChain().scrape_metadata(
                fileitem=schemas.FileItem(
                    storage="local", type="dir", path=item_path,
                    name=target_dir.name, basename=target_dir.stem,
                    extension=None, modify_time=target_dir.stat().st_mtime,
                ),
                mediainfo=mediainfo,
                overwrite=False,
            )
        except Exception as err:
            logger.warning(f"STRM刮削：宿主图片管道修复失败：{str(err)}")
        # 第二轮：仍缺失的用 TMDB 域名回退链直抓 original 高清原图
        missing, _ = self.__bad_images(expected)
        for p in missing:
            self.__tmdb_fallback_download(p.name, mediainfo, p)
        # 全部齐整才算修复成功；否则调用方（体检队列）会回队尾重试
        still_missing, still_broken = self.__bad_images(expected)
        return not still_missing and not still_broken

    def __tmdb_fallback_download(self, name: str, mediainfo: Any, dest: Path) -> bool:
        """按域名回退链从 TMDB 直抓 original 尺寸海报/背景图。"""
        if name == "poster.jpg":
            tmdb_path = getattr(mediainfo, "poster_path", None)
            sizes = ["original", "w780", "w500"]
        elif name in ("fanart.jpg", "backdrop.jpg"):
            tmdb_path = getattr(mediainfo, "backdrop_path", None)
            sizes = ["original", "w1280", "w780"]
        else:
            return False
        if not tmdb_path:
            return False
        tmdb_path = str(tmdb_path).lstrip("/")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        min_bytes = max(int(self._min_image_kb), 1) * 1024
        for domain in [d.strip() for d in self._image_domains.split(",") if d.strip()]:
            for size in sizes:
                url = f"https://{domain}/t/p/{size}/{tmdb_path}"
                try:
                    resp = requests.get(url, headers=headers, timeout=(6, 30))
                    if resp.status_code != 200:
                        continue
                    data = resp.content
                    # 体积校验 + 必须是 JPEG/PNG 魔数，防止把错误页存成图片
                    if len(data) < min_bytes:
                        continue
                    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
                        continue
                    tmp = dest.with_suffix(dest.suffix + ".tmp")
                    tmp.write_bytes(data)
                    tmp.replace(dest)
                    logger.info(f"STRM刮削：回退域名抓图成功 {dest.name} <- {url} ({len(data) // 1024}KB)")
                    return True
                except Exception:
                    continue
        logger.info(f"STRM刮削：回退域名抓图失败 {dest.name}（所有域名不可达）")
        return False

    # ==================== 全库图片体检队列 ====================

    def __build_sweep_queue(self) -> None:
        """全库扫描：把所有缺图/坏图的媒体目录排进修复队列。"""
        exts = [e.strip() for e in self._exts.split(",") if e.strip()]
        excludes = [p for p in self._exclude_paths.splitlines() if p.strip()]
        queue: List[str] = []
        seen_dirs = set()
        for raw in self._watch_paths.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            if raw.count("#") == 1:
                raw = raw.split("#")[0]
            root = Path(raw)
            if not root.exists():
                continue
            for file_path in SystemUtils.list_files(root, exts):
                if self._event.is_set():
                    return
                if self.__is_excluded(file_path, excludes):
                    continue
                mtype = MetaInfoPath(file_path).type or MediaType.MOVIE
                target_dir = file_path.parent if mtype == MediaType.MOVIE else self.__media_target_dir(file_path, mtype)
                dkey = str(target_dir)
                if dkey in seen_dirs:
                    continue
                seen_dirs.add(dkey)
                expected = ([target_dir / n for n in self._MOVIE_IMAGES]
                            if mtype == MediaType.MOVIE
                            else [target_dir / n for n in self._SHOW_IMAGES])
                missing, broken = self.__bad_images(expected)
                if missing or broken:
                    queue.append(dkey)
        self.save_data("sweep_queue", queue)
        logger.info(f"STRM刮削：全库体检完成，共 {len(seen_dirs)} 个媒体目录，{len(queue)} 个待修复")

    def __drain_sweep_queue(self) -> None:
        """每轮消费少量队列目录做图片修复，逐步修完全库。"""
        queue: List[str] = self.get_data("sweep_queue") or []
        if not queue:
            return
        batch = queue[:5]
        self.save_data("sweep_queue", queue[5:])
        failed: List[str] = []
        for dkey in batch:
            if self._event.is_set():
                failed.extend(batch[batch.index(dkey):])
                break
            target_dir = Path(dkey)
            if not target_dir.exists():
                continue
            strms = list(target_dir.glob("*.strm"))
            if not strms:
                continue
            try:
                is_show = (target_dir / "tvshow.nfo").exists()
                probe_file = strms[0]
                mediainfo, mtype, ident = self.__recognize(probe_file, MediaType.TV if is_show else None)
                if mediainfo is None:
                    logger.info(f"STRM刮削：体检跳过 {dkey}（{ident}）")
                    continue
                if self.__repair_images(probe_file, mtype, mediainfo):
                    self.__bump("imgfix")
                else:
                    # 网络不可达等原因未修好：回队尾，下批再试
                    failed.append(dkey)
            except Exception as err:
                logger.warning(f"STRM刮削：体检修复失败 {dkey}：{str(err)}")
                failed.append(dkey)
        if failed:
            remaining = self.get_data("sweep_queue") or []
            remaining.extend(failed)
            self.save_data("sweep_queue", remaining)
        left = len(self.get_data("sweep_queue") or [])
        logger.info(f"STRM刮削：体检队列本批处理 {len(batch)} 个（回队 {len(failed)}），剩余 {left} 个")

    # ==================== 工具方法 ====================

    def __mark_seen(self, seen: Dict[str, dict], file_path: Path, done: bool) -> None:
        """更新文件去重记录。"""
        try:
            stat = file_path.stat()
        except OSError:
            return
        key = str(file_path)
        record = seen.get(key, {})
        seen[key] = {
            "m": stat.st_mtime, "s": stat.st_size,
            "d": True if done else record.get("d", False),
            "f": 0 if done else record.get("f", 0) + 1,
        }
        if len(seen) > 20000:
            for k in sorted(seen.keys())[:5000]:
                seen.pop(k, None)

    @staticmethod
    def __is_excluded(file_path: Path, excludes: List[str]) -> bool:
        """判断文件是否位于排除目录。"""
        for exclude in excludes:
            try:
                if file_path.is_relative_to(Path(exclude.strip())):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def __nfo_exists(file_path: Path, mtype: MediaType) -> bool:
        """判断对应 NFO 是否已存在。"""
        if mtype == MediaType.MOVIE:
            folder = file_path.parent
            return (folder / "movie.nfo").exists() or (folder / f"{file_path.stem}.nfo").exists()
        return file_path.with_suffix(".nfo").exists() or (file_path.parent / "tvshow.nfo").exists()

    def __bump(self, kind: str) -> None:
        """累计统计计数。"""
        stats = self.get_data("stats") or {}
        stats[kind] = stats.get(kind, 0) + 1
        self.save_data("stats", stats)

    def __append_history(self, item: dict) -> None:
        """追加历史记录，保留最近300条。"""
        history = self.get_data("history") or []
        history.append(item)
        self.save_data("history", history[-300:])

    def __to_emby_path(self, local_path: Path) -> str:
        """本地路径按映射规则转换为 Emby 侧路径。"""
        text = str(local_path).replace("\\", "/")
        for rule in self._path_replace.splitlines():
            if "#" not in rule:
                continue
            src, dst = rule.split("#", 1)
            if text.startswith(src.strip()):
                return dst.strip() + text[len(src.strip()):]
        return text

    def __emby_refresh_after(self, file_path: Path, mtype: MediaType, mediainfo: Any) -> None:
        """刮削/修复成功后触发 Emby 刷新。"""
        if not (self._refresh_emby and self._emby_url and self._emby_api_key):
            return
        media_dir = file_path.parent if mtype == MediaType.MOVIE else self.__media_target_dir(file_path, mtype)
        self.__emby_refresh(media_dir, getattr(mediainfo, "title", "") or "")

    def __emby_refresh(self, media_dir: Path, title: str) -> None:
        """按标题搜索 Emby 条目，用路径前缀匹配本次刮削目录后触发单条刷新。"""
        try:
            emby_dir = self.__to_emby_path(media_dir).replace("\\", "/")
            if not title:
                return
            resp = requests.get(
                f"{self._emby_url}/Items",
                params={"searchTerm": title, "recursive": "true", "fields": "Path",
                        "includeItemTypes": "Movie,Episode", "limit": "20",
                        "api_key": self._emby_api_key},
                timeout=10)
            items = (resp.json() or {}).get("Items") or []
            hit = 0
            for item in items:
                item_path = (item.get("Path") or "").replace("\\", "/")
                if item_path.startswith(emby_dir):
                    requests.post(
                        f"{self._emby_url}/Items/{item.get('Id')}/Refresh",
                        params={"api_key": self._emby_api_key},
                        timeout=10)
                    hit += 1
                    logger.info(f"STRM刮削：已触发Emby刷新 {item.get('Name')} ({item.get('Id')})")
            if not hit:
                logger.info(f"STRM刮削：Emby 中尚未收录 {emby_dir}，等待其下次扫描")
        except Exception as err:
            logger.warning(f"STRM刮削：Emby刷新失败（不影响NFO）：{str(err)}")

    def stop_service(self) -> None:
        """停止插件后台服务。"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as err:
            logger.error(f"STRM刮削：停止服务异常：{str(err)}")
