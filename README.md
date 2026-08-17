# ASMR 收藏馆

面向授权公开页面的保守型标签归档器。当前配置从
`https://asmrlib.com/tags/yoonying` 发现帖子，并将详情页保存为严格无广告的静态归档。

## 工作方式

1. 从配置的 `/tags/<slug>` 页面开始。
2. 只接受同域 `/posts/<32 位十六进制 ID>` 链接。
3. 只跟随同一标签的显式分页链接，并执行页数、帖子数、重复内容和循环限制。
4. 使用纯 HTTP 获取主文档，不执行页面 JavaScript，也不加载广告子资源。
5. 从响应中提取白名单字段，丢弃原始 DOM，再重新生成没有脚本和远程资源的静态 HTML。
6. 仅下载通过域名、扩展名和 Content-Type 校验的直接媒体。

## 快速开始

```powershell
cd D:\project\asmrlib_archiver
uv venv .venv --python 3.13
uv --cache-dir .uv-cache pip install --python .venv\Scripts\python.exe -e .
.\.venv\Scripts\asmrlib-archiver.exe run seeds.txt
.\.venv\Scripts\asmrlib-archiver.exe status
```

`run` 会依次执行：净化旧归档、发现标签分页、抓取新帖子、下载允许的媒体。
这是一次性批处理命令。本地阅览请另开：

```powershell
.\.venv\Scripts\asmrlib-archiver.exe serve
```

## 配置标签

`config.yaml`：

```yaml
seeds: []

# 往 tag_seeds 加更多 /tags/<slug> 即可发现更多帖子；
# 本地收藏馆的「实时浏览」也会把这些标签当成快捷入口。
tag_seeds:
  - "https://asmrlib.com/tags/yoonying"
  # - "https://asmrlib.com/tags/another-tag"

discovery:
  enabled: true
  max_pages_per_tag: 100
  max_posts_per_tag: 2000
  page_batch_size: 20
  max_page_bytes: 5242880
```

`seeds.txt` 也可以混合填写严格格式的标签 URL 和帖子 URL。重复 URL 会由 SQLite 自动去重。

## 命令

```powershell
# 加入 config.yaml 和文件中的种子
.\.venv\Scripts\asmrlib-archiver.exe add seeds.txt

# 仅发现标签页中的帖子
.\.venv\Scripts\asmrlib-archiver.exe discover

# 仅抓取已发现的帖子
.\.venv\Scripts\asmrlib-archiver.exe crawl --limit 100

# 重新抓取已归档帖子，刷新播放器引用 / 直链媒体
.\.venv\Scripts\asmrlib-archiver.exe crawl --refresh --limit 200

# 仅回填缺失的封面（不改 HTML / 不动 media_candidates）
.\.venv\Scripts\asmrlib-archiver.exe crawl --covers-only --limit 200

# 将旧的原始 HTML 替换为安全静态归档
.\.venv\Scripts\asmrlib-archiver.exe sanitize

# 完整流程；失败或安全上限截断时返回非零退出码
.\.venv\Scripts\asmrlib-archiver.exe run seeds.txt --retry-errors

# 导出已归档目录（元数据 + 播放器引用目录；不下载受保护媒体）
.\.venv\Scripts\asmrlib-archiver.exe export --format json
.\.venv\Scripts\asmrlib-archiver.exe export --format csv --output .\data\exports\catalog.csv
.\.venv\Scripts\asmrlib-archiver.exe export --format markdown --tag yoonying

# 把你已拥有的本地文件挂到某条帖子上，本地收藏馆即可播放
.\.venv\Scripts\asmrlib-archiver.exe import-media --source <post-url或32位id> --file D:\owned\track.mp3

# 本地阅览（只读收藏馆，默认仅本机）
.\.venv\Scripts\asmrlib-archiver.exe serve
# 浏览器打开 http://127.0.0.1:8765/
# /browse  = asmrlib 首页实时预览（最新投稿，可翻页）
# /explore = 按标签实时浏览
# 详情页在线内容优先由桌面版受控播放器打开（自动拦截弹窗与广告跳转）；
# 普通 serve 模式会回退到系统默认浏览器
# /author  = 按作者浏览；详情页底部有同标签相关推荐
```

## 能看 / 能播说明

ASMRLIB 详情页通常只提供第三方 iframe 播放器（如 BI / UP），**没有同域直链 mp4/mp3**。  
归档器会：

- 记录这些播放器引用（状态 `reference`）；桌面详情页提供 **在线播放 / 安全播放**，自动拦截常见弹窗与广告跳转，并保留 **原链** 直达入口；
- **不会**解析、绕过、解密或批量下载第三方播放器内容；
- 仅当页面出现通过域名 / 扩展名 / Content-Type 校验的**直接媒体 URL** 时才下载到 `data/videos/`；
- 你可以用 `import-media` 把**已经拥有**的本地文件挂到某条帖子上，之后 `serve` 详情页用 `<video>` / `<audio>` 真正播放（支持 Range 拖动进度）；
- `export` 可把帖子元数据 + 播放器引用目录导出为 JSON / CSV / Markdown。

### 想真正播起来时怎么做

1. **立刻听 / 看（在线）**  
   桌面版打开帖子 → 点 **在线播放**；普通 `serve` 模式会回退到系统浏览器。
2. **离线本地播（你已有文件）**  
   ```powershell
   .\.venv\Scripts\asmrlib-archiver.exe import-media `
     --source https://asmrlib.com/posts/<32位hex> `
     --file D:\path\to\owned.mp3
   .\.venv\Scripts\asmrlib-archiver.exe serve
   ```
3. **站点给了直链**（少见）  
   `download` / `run` 会自动落到 `data/videos/`。

因此：「能看」= 元数据 + 引用目录 + 一键打开原播放器；「能本地播」= 直链下载 **或** `import-media` 挂上你已有的文件。  
当前若 `media.downloaded=0`，说明还没有本地文件——用上面 1 或 2，而不是等破解。

## 去广告保证

归档 HTML 不复制站点页面，而是从结构化字段重新生成。写盘前必须通过以下检查：

- 包含项目安全标记和严格 CSP；
- 不包含 `script`、`iframe`、`object`、`embed`、表单或事件属性；
- 不包含 `href`、`src`、`srcset` 等可触发远程请求的属性；
- 元数据不包含已知广告、推广和统计域名；
- 浏览归档文件时不会发起第三方请求或打开弹窗。

遇到未知页面结构、跨域分页、重复页面或达到安全上限时，任务会标记为失败或
`incomplete`，不会假报完整。

## 数据目录

- `data/archive.sqlite3`：标签队列、标签与帖子关系、帖子及媒体状态。
- `data/html/`：重新生成的安全静态归档。
- `data/metadata/`：白名单结构化元数据。
- `data/videos/`：验证后下载的媒体文件。
- `data/exports/`：`export` 命令生成的目录快照（默认路径）。

## 桌面版

桌面产品只有一个轻量版本：本机 `serve` + **pywebview** 主窗口。本地媒体可以在应用内播放，并可打开 always-on-top 小窗；在线内容优先在系统 WebView2 的受控播放器中打开，自动拦截常见广告请求、弹窗和越界跳转，失败时回退到系统默认浏览器。产物不包含 Playwright、内置 Chromium、在线录制或代理播放器。

```powershell
# 安装桌面依赖
uv --cache-dir .uv-cache pip install --python .venv\Scripts\python.exe -e ".[desktop]"

# 开发态直接跑（无需打包）
.\.venv\Scripts\python.exe desktop_main.py

# 唯一构建入口
.\.venv\Scripts\python.exe build_light.py

# 仅检查 PyInstaller 参数，不生成或替换产物
.\.venv\Scripts\python.exe build_light.py --dry-run
```

说明：

- 输出固定为 `dist/asmrlib-archiver/asmrlib-archiver.exe`。
- 配置与数据在 **exe 同目录** 的 `config.yaml` / `data/`，便于迁移和备份。
- 重建时优先保留现有 `dist/asmrlib-archiver` 的数据；首次使用新名称时，会继承旧 `dist/asmrlib-light` 的 `data/` 与 `config.yaml`，且不会删除旧目录。
- 若两处产物都没有有效数据库，构建脚本才会使用项目根目录的 `data/` 和 `config.yaml` 作为种子。
- 旧 `build_desktop.py` 只作为兼容转发脚本保留，不再构建另一种产品。
- 构建日志分别报告运行时大小与包含用户数据后的总大小；60 MiB 目标以 `_internal/` 加 exe 的运行时大小为准，持续增长的 `data/` 不计入。
- Windows 需已安装 **WebView2**（Win11 通常自带）；它负责受控在线播放，不是随包附带的 Chromium。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 合规边界

项目仅用于归档你有权访问和保存的公开页面。它不登录、不模拟会员权限、不携带账户
Cookie、不绕过签名、防盗链或 DRM，也不会解析任意第三方播放器来规避访问控制。
