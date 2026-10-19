# Design System — asmrlib archiver 移动端

> 参考格式：[VoltAgent/awesome-design-md](https://github.com/VoltAgent/awesome-design-md)。本文档是本项目移动端正负设计系统的唯一事实来源（single source of truth 的实现见 §1 的"双镜像"约定）。改视觉效果先改这里，再改代码。

## 1. 视觉基调 & 氛围

应用是一个**暗色沉浸的 ASMR 音/视频壳**。设计哲学是"内容先行的黑暗"（content-first darkness，参考 Spotify 的 DESIGN.md）：界面退入近黑的炭色里，封面图与正在播放的画面负责发光；品牌蓝紫（`#7c9cff → #9b7cf0`）只出现在功能性的地方——进度、主按钮、选中态——绝不做装饰。

整体气质对齐 EaraAsmrPlayer（上游参考实现，见 `D:/project/EaraAsmrPlayer`）的"正在播放"页：圆角大、克制、手指友好的 pill / 大圆几何，配合分层玻璃质感（GlassDrawable 的 rim-light 叠层）营造一台"高级音频设备"的触感。

**关键特征**
- 全站**钉死深色**（`#0a0b0f` 系）——OS 浅色模式也不翻转；`Theme.isNight()` 硬编码 `true`，CSS 在 `prefers-color-scheme: light` 里原样重声明同一组色值
- 唯一品牌强调色 `#7c9cff`（辅 `#9b7cf0`），Web (CSS) 与原生 (Theme.java) 必须是**同一个数字**——`tests/test_mobile.py::test_the_web_and_native_accents_are_the_same_colour` 会卡住漂移
- 圆角语言：卡片 `18px`、封面 `28px`、chip `10px`、按钮 `24px`、pill `999px`
- 封面即氛围：播放页用封面模糊放大做整页背光（Eara 的 backdrop 叠层）
- 自绘 chrome：所有原生图标/进度条/环都是手绘 Canvas（`PlayerGlyph`/`PlayerSeekBar`/`LoadingRing`），不许引入带平台主题的控件（stock `MediaController` 就是前车之鉴）

## 2. 颜色 & 角色

实现入口：`android/app/src/main/java/com/asmrlib/archiver/Theme.java`（原生）与 `android/app/src/main/assets/{discover,play}.html` 顶部 `:root` CSS 变量（Web），两侧逐值镜像。

### 主品牌
| 角色 | 值 | 用途 |
|---|---|---|
| Accent 主蓝 | `#7c9cff` | 进度、主按钮、选中态、链接 |
| Accent 辅紫 | `#9b7cf0` | 渐变尾端（SweepGradient）、强调描边 |
| On-Accent 文字 | `#06101f` | accent 底上的文字 |

### 表面（全部由近黑炭色构成）
| 角色 | 值 |
|---|---|
| 页面底 `--bg` | `#0a0b0f` |
| 表面 `--surface` | `#15171e` |
| 表面 2 `--surface-2` | `#1e212a` |
| 表面 3 `--surface-3` | `#262a34` |
| 导航底 `--nav-bg` | `#0e1016` |
| 玻璃导航 `--glass-nav` | `rgba(17,20,29,.82)` + `--glass-edge: rgba(232,240,255,.24)` |

### 文字
| 角色 | 值 |
|---|---|
| 主文字 `--text` | `#f1f3f8` |
| 次文字 `--text-2` | `#a2acbd` |
| 弱文字 `--text-3` | `#6e7a8c` |

### 填充 / 分隔
`--fill: rgba(255,255,255,.065)` / `--fill-strong: .12` / `--sep: rgba(255,255,255,.085)` / `--sep-strong: .17`

### 语义色
| 角色 | 值 |
|---|---|
| 危险 `--danger` | `#ff7a7a`（`DANGER_ALT #DC6FBF`） |
| 成功 `SUCCESS` | `#5AD39A` |
| 警告（下载失败 badge） | `rgba(250,204,21,*)` |

### 播放器背光叠层（Eara backdrop stack）
页面渐变（SURFACE→BG）→ **色相薄雾**（封面 16×16 均值转 HSV，S 钳制 0.24–0.58、V 钳 0.42–0.56；灰封面强制 hue=228 回品牌蓝）→ **模糊封面**（两次双线性降采样至 1/16→1/5，40% 黑罩）→ **scrim**（`argb(120,0,0,0)`，音频页 96）。任何一层只在有封面时出现——没有封面的页不许来路不明地上色。

### 阴影
玻璃质感用 `GlassDrawable` 叠层（体渐变 `0xF0383E4C→0xF01B202D→0xF0141821` + 左上 radial 高光 `0x27C6D8FF` + rim 描边 `0xA9EFF5FF/0x19C8D6F1/0x4C9BB7EE` + 按压罩 `0x16FFFFFF`），不用平台 elevation 阴影。

## 3. 字体规则

字体栈（双端一致）：`-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, "Segoe UI", Roboto, "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif`

| 角色 | 字号 | 字重 | 用途 |
|---|---|---|---|
| 大标题 `--fs-large-title` | 30px | 700 | 发现页标题 |
| 标题 `--fs-title` | 21px | 700 | 分节标题 |
| 胡子标题 `--fs-headline` | 16px | 700 | 卡片标题、hero 标题（play 页 23px/700 + text-shadow） |
| 正文 `--fs-body` | 15px | 400 | 列表主行 |
| 副标题 `--fs-subhead` | 13.5px | 400/700 | 表格值（700）、按钮字 |
| 脚注 `--fs-footnote` | 12.5px | 400 | 表格标签、次信息 |
| 说明 `--fs-caption` | 11.5px | 400 | 时间轴刻度、提示行（进度时间用 `Typeface.MONOSPACE`） |

原则：**字重对比造层级，不靠字号堆**——常规 400 / 强调 700 二元为主。中文优先用短词（"上一轨/下一轨/队列"），四词连排是设置页，不是工具栏。

## 4. 组件样式

### 播放按钮（PlayPauseButton，原生自绘）
- accent 渐变圆角块，尺寸：传输条 52dp / 音频页 72dp（对应 cell 84×72）
- 播放↔暂停用**圆角 44%↔30% 的形变**做过渡（Eara 的 36dp↔24dp / 220ms 方案），而不是生切系统 ▶⏸ 图标

### 传输按钮（ImageButton + iconPlate）
- 背板：`argb(30,255,255,255)` 填充 + `argb(46,255,255,255)` 描边，圆角 21dp
- cell 规格：音频页 ±10s/上一轨/下一轨/息屏 `64×52`，播放键 `84×72`；视频页 `56×42` / 播放 `52×52`；**每个 cell 左右等距 4dp**——均匀是构造出来的，不靠记
- glyph 线宽 `s*0.085`，圆头 stroke（`PlayerGlyph`）

### 视频浮层 chip（chipButton）
- `argb(89,0,0,0)` 黑底 + 圆角 10dp、20dp glyph、40×40 —— Eara 的贴画 chip，长在视频上而不是工具栏里（填充/全屏/倍速）

### 进度轨（PlayerSeekBar，自绘）
- 未播：弱填充；已播：`#7c9cff→#9b7cf0` 渐变；旋钮带内点；拖拽时旋钮放大
- 时间标签 `11.5sp` MONOSPACE，分居轨两端；时长未知时轨道停用、显示 `--:--`（不许造假满轨）
- 拖拽只更新标签，松手才 seek（对串联 TS，逐帧 seek 会把扫描堆成队列）

### 卡片 / 表面
- 详情卡 `darkSurface(190)` + 18dp 圆角 + `darkAccent(30)` 描边
- 传输卡 `Theme.darkBg(158)`
- 错误板 `Theme.darkBg(235)` + 居中
- 浮层气泡（拖拽快进）`Theme.darkBg(200)` + `0x30FFFFFF` 描边、22dp 圆角

### 列表页（discover.html）
- CSS columns:2 瀑布流、1:1 封面、右下角时长 pill、accent 标源站、#tag 行
- ASMR.one 镜像线路 chips 持久化于 `localStorage['ds-asmrone-site']`
- 顶部吸顶玻璃导航 52px、首次进页 96px 旋转环 + 56px 品牌 glyph 启动幕
- **源切换是原地过渡，不是导航**（本站 ⇄ ASMR.one）：旧卡片留在屏幕上并整块压暗（`.is-switching` → `opacity:.38`，卡片墙/标签行/分节标题一起），绝不换成骨架；搜索框与线路行按 `max-height` 折叠/展开（各占位 40px/30px + 12px 外边距，故约 94px），不再用 `display:none` 一帧瞬移；滚回顶部走平滑滚动，落定的那一刻才换内容。失败路径（错误面板）同样解除压暗。降级见 §6

### 播放源行（play.html，统一播放样式）
一行一个源，**只有一种行**：应用内播放行。`kind` 只决定点按之后发生什么，不再决定行长什么样——旧源（本站 BI/UP/AB 的嵌入播放页）不再是那个「点出去」的例外：

- `hls` / `direct`：URL 就是媒体字节，整行与 accent 胶囊「应用内播放」直接走 `playDirect*` 进统一播放器。
- `embed`：URL 是第三方播放器**页面**。点按先走桥的 `resolveEmbed`——隐藏 WebView 加载该页（广告盾拦在每个请求上），把运行中的播放器真实拉起的媒体流抓回来，随后与 `hls`/`direct` 同路进统一播放器。解析期间行复用 `is-loading` 转环；**解析失败该行才降级**为普通链接行（meta 写 `网页播放`、行尾 `›`），即统一化之前的旧行为——降级是兜底，不是第二种常态。

- 行的 `href` **必须不存在**：媒体行的 href 就是字节本身，WebView 一落上去整页就被 Chromium 自带的播放页顶替（标题、列表、返回全没），这正是「asmr.one 点不了」的根因；embed 行同样不给 href——给了一跳就绕过解析，把第三方页面顶回统一播放器之上。锚点只允许出现在降级行与页脚 chips。
- 行内带 `data-action` 的元素（胶囊、`+`）自己处理点按，整行处理器让开。
- 连播队列只收已经解析出字节的源：embed 行没有「+」，聚合条只统计 hls/direct；embed 解析成功时走单条队列同一入口，嗅探到的 Referer 以 `rr` 固定字段直通播放器。
- 行尾两行文字各自单行省略（`.src-name` / `.src-meta`）；标签只写轨名，文件夹路径走 `group` 字段，且仅在同名轨并存时才显示最深层那段。
- hero 顶部来源标识 `.pl-hero-src`（黑玻璃 chip，同画面上的其它 chip）：`ASMR.one` / `本站`，四台镜像折成一个名字。

## 5. 布局原则

- 间距基准 4px 级：`--sp-1..7 = 4/8/12/16/20/24/32`；页面内边距 `--inset: 16px`
- 触控目标 ≥44dp（`--tap: 44px`）；封面比例 1:1（`--thumb-ratio`）
- 圆角梯度：`8/12/16/20/24/26/28(封面)/999(pill)`
- 播放页**竖屏**：16:9 舞台 + 下方可滚详情列；**横屏**：舞台吃满全窗、详情列消失、沉浸模式隐藏系统栏（竖着的视频被系统栏夹着是"最没适配感"的事）
- 平板断点（Web 页）：768–899px 居中 760px 栏 + 16:9 盒装 hero；≥900px 双列（左 sticky hero，右源列表，max 1120px）

## 6. 动态 / 手势

| 参数 | 值 | 出处 |
|---|---|---|
| 按压 | `.13s` | `--dur-press` |
| 快变 | `.18s` | `--dur-fast`（控制条 fade 用 180ms） |
| 常规 | `.26s` | `--dur` |
| 入场 | `.42s` / `360ms` | `--dur-enter`；NowPlayingMotion 360ms / 90ms stagger / 40dp rise（`DecelerateInterpolator(1.6)`） |
| 缓动 | `cubic-bezier(.32,.72,0,1)` | `--ease` |
| 往返 | 播放键形变 220ms | Eara PlayPauseButton |

- 视频手势：轻点切换控制条（4s 自动隐藏，`CONTROLS_TIMEOUT_MS`）；**横向拖动=滑动快进**（满宽 ≈ ±360s），拖动时中间浮气泡「快进 2:31 · 5:40」，松手才提交
- 系统 `prefers-reduced-motion` / `prefers-reduced-transparency` 在 Web 两侧皆有降级处理：减少动效下**不新增位移**——卡片入场与按压位移取消，源切换只保留淡出（`max-height` 折叠停掉），滚回顶部与分页跳转从 `smooth` 改 `auto`。**但不能一刀切砍掉过渡**：下拉弧的 `stroke-dashoffset` 承载着下拉距离这个信息，必须保留

## 7. 播放页专章（本次迭代的标尺）

以 Eara `NowPlayingScreen.kt` / `NowPlayingControls.kt` / `NowPlayingProgress.kt` 为参照系：

- **音频页** = Classic now-playing：折叠头（左返回+居中标题）→ 方形大封面（clamp 180–360dp、28dp 圆角、浮于背光之上，无卡无边）→ 身份块（标题 2 行 18sp/700 + 元信息行）→ 无底卡进度轨 → 传输排（上一轨/后退10s/播放(放大)/前进10s/下一轨）+ 第二排工具（倍速 chip、队列、息屏）。封面是页面上唯一没有自然高度的事物：宽度上限由 `audioCoverSide()` 给，高度**在布局后按身份块实测需求反推**（`fitAudioCover`），所以再加一排控件也不会把标题挤没
- **Web 播放页 hero** 带来源标识 chip（`.pl-hero-src`，`ASMR.one` / `本站`）：两个源行为不同，页面必须自报家门，不能靠行内箭头暗示
- **视频页** = 贴画 chrome：顶栏只有返回+标题（透明无板）；填充/全屏/倍速是贴画 chip（40×40 黑罩）；底部一条自绘传输卡随点按淡出
- **队列**：有队必有"上一轨/下一轨"；asmr.one 数据源在音轨行尾部给"加入连播"小钮（Eara QueueSheet 的 `addedToQueue` toast）；超长队列（>30）在页面给"全部连播"聚合入口（从指定轨起），不在播放器上堆
- **倍速**：音频页第二排 chip、视频页贴画 chip，循环 `0.75→1.0→1.5→2.0`；`MediaPlayer.setPlaybackParams`（API 23+），失败静默回落 1.0
- **息屏**：月亮键交给 `PlaybackService`（含队列+续播键），页面 finish——息屏连播是既有能力，不做睡眠定时（Eara 的 SleepTimer 暂不引入）

## 8. 既有坑（勿重蹈）

- 代理必须绑 IPv4 回环 + `nonProxyHosts` 豁免（见 memory: asmrlib-eara-reference-and-stream-proxy）
- LinearLayout 做 overlay 会把 weight 子项挤成 0——音频页 overlay 必须是 FrameLayout 兄弟
- `VideoView`/`MediaController` 禁用（见 PlayerActivity 类注释）
- 单次失败不等于失败：transient 错误自动重试 ×3、带 5s 可见倒计时，穷尽才上面板
- **媒体 URL 不能当 href**：WebView 落到音频字节上会把整页换成 Chromium 自带的播放页——没有标题、没有列表、没有返回。所有源行都是按钮（`data-inapp`），embed 行也不例外——它的点按是先解析；锚点只属于降级行与页脚 chips。见 §4 播放源行
- **隐藏 WebView 嗅探（resolveEmbed）的六条底线**：①广告盾拦在每个请求上再分类，广告的 `.mp4` 不得冒充「检测到的媒体」；②声音：WebView 手势门槛放开 + 页面内强制静音（`HTMLMediaElement.play` 补丁在 `onPageStarted` 先行注入）+ 媒体地址一分类立即 `onPause` 灭声；隐藏页面允许自动播放，但**永远不许出声**；③启动流：跨域 iframe 里的播放钮不理会合成点击，只有真实输入事件才有 user activation——隐藏 WebView 以真实尺寸屏外挂载（`alpha 0`、`translation -9999`），嗅探节奏里用 `dispatchTouchEvent` 往中心送一次真触摸，再配合定时戳通用播放器按钮；④外壳+iframe 是站点本来的形状：bysetayico /e/{id} 把真播放器嵌在跨域 iframe（轮换域 /{route}/{id}）里——嗅探拦截器为子框文档先过 `trustEmbeddedPlayerUrl` 信任门（这条 API 直到这个嗅探器才有生产调用方），iframe 放行后才会拉流；不跳顶框、不跨 frame 注入；⑤只认公网 http(s)，回环/内网地址不算媒体；嗅探结果 TTL 只有 5 分钟（CDN 签名 token 会过期）；⑥Referer 用播放页自己发的那一个（队列条目 `rr` 固定字段直通 StreamProxy），不许再过 `refererFor` 启发式
- **高度预算必须是量出来的，不是常数**：音频页封面曾经按 `heightPixels - dp(300)` 定尺寸，底部加一排工具行后 640dp 屏把身份区标题压成 33px 的一条、meta 行归零（LinearLayout 溢出是**裁掉**，不是省略号）。现在 `audioCoverSide()` 只给宽度上限，高度由 `fitAudioCover()` 在布局后用**不受约束**的测量值算
- **内容不该塞进「名字」字段**：asmr.one 的轨 label 曾把整条文件夹路径拼进去，于是播放页每行 5 行高、队列行只剩 `06: 特典 · 03: 闹钟音频 · 01_mp3 · 0…`。路径走 `group`，名字只写名字
