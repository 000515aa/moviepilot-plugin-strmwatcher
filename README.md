# StrmWatcher —— MoviePilot STRM 实时刮削插件

为 **网盘 strm 媒体库**（115/123/Alist 等生成的 `.strm` 文件）设计的 MoviePilot v3 插件：
新 strm 文件落盘后 **分钟级** 自动完成「识别 → 刮削 NFO → 下载高清海报/背景图 → 通知 Emby 单条刷新」，
并对存量库提供**图片体检修复**能力，解决 strm 场景下媒体服务器在线刮削失败导致的"裂图/无元数据"问题。

## 为什么需要它

- strm 文件指向网盘 URL，Emby/Jellyfin 的**在线元数据抓取经常失败**（探测流信息超时、图片源不可达），
  表现为影片无海报、无简介，或海报显示为裂图后回退到不可用的在线源。
- 传统做法依赖定时全库刮削（小时级），新片入库后长时间"裸奔"。
- 本插件把刮削时机前移到 **strm 落盘那一刻**，并把元数据以本地 NFO + 图片文件的形式写死在媒体目录里，
  媒体服务器只需"读取"，不再"在线刮"。

## 功能特性

- **增量实时监控**：高频（默认 2 分钟）扫描监控目录，只处理新增/变更的 strm 文件；
  文件静默等待（防抖）避免写入半截文件。
- **精准识别**：优先采信目录名中的 `{tmdb-xxxx}` / `{tmdbid=xxxx}` 标识（MoviePilot 整理格式），
  无标识时按文件名识别；识别结果缓存复用宿主能力。
- **完整刮削**：复用 MoviePilot 核心刮削链（TMDB + Fanart.tv + 豆瓣 多源合并），
  写入 Kodi 格式 NFO 与全套图片（poster/fanart/backdrop/logo/banner/clearart/landscape…）。
- **图片体检修复（v1.1）**：即使 NFO 已存在，也会检查 `poster.jpg / fanart.jpg / backdrop.jpg`
  是否缺失或体积过小（半截文件/错误页），自动删除坏文件并重抓 **TMDB original 高清原图**；
  内置**域名回退链**（`image.tmdb.org → theimage.tmdb.org → tmdb.img.cafe`，可自定义），
  下载后做体积 + JPEG/PNG 魔数校验、原子写入。
- **全库图片体检（v1.1）**：一次性开关，扫描所有监控目录，把缺图/坏图目录排入队列，
  每轮限量修复、失败自动回队尾重试，逐步消化存量缺口。
- **Emby 单条刷新**：刮削/修复成功后按"标题搜索 + 路径前缀匹配"精确定位 Emby 条目并触发刷新，
  新片秒级带海报入库；Emby 尚未收录时优雅降级等待下次扫描。
- **安全护栏**：
  - 首轮运行只登记存量基线、不刮削（可显式开启存量处理）；
  - 已有 NFO 默认跳过（可切覆盖模式）；
  - 识别结果无 TMDB ID 时主动跳过，规避 MoviePilot v3 核心
    `TheMovieDbModule.obtain_images` 的断言缺陷（该缺陷会对 IMDb/豆瓣身份抛
    `AssertionError` 并触发系统错误通知风暴）；
  - 单文件失败重试上限 5 次，不无限重试。
- **可观测**：插件详情页展示累计成功/失败/跳过/图片修复计数、体检队列剩余数、最近 300 条处理历史。

## 安装

### 方式一：插件市场（推荐）

MoviePilot 设置 → 插件 → 插件市场 → 添加仓库地址：

```
https://github.com/000515aa/moviepilot-plugin-strmwatcher/
```

然后在市场中安装「STRM实时刮削」。

### 方式二：本地插件源

1. 将本仓库克隆到 MoviePilot 可访问目录，例如 `/config/local-plugins`；
2. 设置环境变量或系统设置 `PLUGIN_LOCAL_REPO_PATHS=/config/local-plugins`；
3. 插件页 → 市场 → 刷新后即可看到本插件，安装即可。

## 配置说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| 启用插件 | 关 | 总开关 |
| 扫描周期 | `*/2 * * * *` | 增量扫描 cron |
| 监控目录 | 空 | 每行一个目录，可加 `#电影` / `#电视剧` 强制类型 |
| 排除目录 | 空 | 每行一个目录（建议排除下载收件箱） |
| 覆盖模式 | 已有NFO则跳过 | 或 `覆盖全部元数据` |
| 文件静默等待 | 30 秒 | 新文件 mtime 距今不足该值时本轮跳过 |
| 单次最多处理 | 50 | 单轮新增文件上限 |
| 体检修复缺失/损坏图片 | 开 | NFO 已存在时也检查并修复图片 |
| 图片最小KB | 20 | 低于该体积视为损坏 |
| 立即全库图片体检 | 关 | 一次性开关，扫描全部监控目录排队修复 |
| TMDB图片域名回退链 | `image.tmdb.org,theimage.tmdb.org,tmdb.img.cafe` | 逗号分隔，按序回退 |
| 刮削后刷新Emby | 关 | 开启后需填 Emby 地址与 API Key |
| 路径映射 | 空 | 每行 `本地前缀#Emby前缀`，容器路径不一致时使用 |

## 典型部署拓扑

```
115/123网盘 ──CMS/整理工具──▶ 本地媒体目录（.strm + 分类目录）
                                  │ 落盘
                                  ▼
                     MoviePilot + StrmWatcher（本插件）
                     识别 → NFO/高清图片写入媒体目录
                                  │
                                  ▼
                     Emby 读取本地元数据（单条刷新即时生效）
```

容器场景下建议 MoviePilot 与 Emby 将同一媒体目录挂载到**相同容器路径**（如都挂 `/media`），
可免去路径映射配置。

## 已知兼容性问题

- MoviePilot v3.0.0 核心 `app/modules/themoviedb/__init__.py::_build_image_query`
  存在未防护的 `assert tmdbid is not None`，识别结果为 IMDb/豆瓣等无 TMDB ID 身份时会抛异常。
  本插件已内置守卫规避；若你在其他插件（如媒体库刮削）中遇到
  "TheMovieDb运行失败" 通知风暴，根因即此，建议将收件箱目录从其刮削路径中排除。
- `SCRAP_SOURCE` 配置为多源链（如 `themoviedb,douban,bangumi`）时，宿主 NFO 模块要求
  `mediainfo.scrape_source` 与识别源精确匹配，否则只写图片不写 NFO；本插件已显式锁定识别来源。

## 开发

- 宿主契约：`app/plugins/__init__.py::_PluginBase`（MoviePilot v3）
- 刮削链：`app.chain.scraping.ScrapingChain.scrape_metadata`
- 识别：`self.chain.recognize_media` / `app.sdk.media.MetaInfoPath`
- 本地调试：放入 `PLUGIN_LOCAL_REPO_PATHS` 后 `GET /api/v1/plugin/install/StrmWatcher?force=true` 热重装

## License

MIT © 2026 000515aa
