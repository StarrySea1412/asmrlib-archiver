# ASMR 收藏馆 · Android APK 壳

零第三方依赖的 WebView 壳：加载电脑上运行的 `asmrlib-archiver serve`，
启动时先做网络测试（连不上给出排查清单），内置与桌面版同源的广告拦截
（`assets/blocklist.json`，由 `python -m asmrlib_archiver.mobile_blocklist`
从 `security_lists.py` 生成，勿手改）。

完整方案与验收标准见 `docs/mobile.md`。

## 构建

- **推荐：GitHub Actions** — 推送后运行 `android-apk` 工作流，Artifacts 下载 `app-debug.apk`。
- **本机**：用 Android Studio 打开本目录（自带 JDK），或：

```powershell
cd android
gradle assembleDebug      # 需 JDK 17 + Android SDK (platforms;android-34)
```

## 安装与测试

```powershell
adb install app\build\outputs\apk\debug\app-debug.apk
adb shell am start -n com.asmrlib.archiver/.MainActivity
adb logcat -s asmrlib-shield     # 观察广告拦截计数
```

- 模拟器默认服务器地址 `http://10.0.2.2:8765`（即宿主机 127.0.0.1）；
  真机改成电脑局域网 IP，如 `http://192.168.1.5:8765`。
- 电脑侧必须 `serve --host 0.0.0.0`，手机与电脑同一 Wi-Fi。

## 结构

```
app/src/main/
  AndroidManifest.xml            # INTERNET + cleartext（局域网 http）
  java/com/asmrlib/archiver/
    MainActivity.java            # 网络门（手机网络测试）+ WebView 壳
    ShieldRules.java             # 桌面 ShieldPolicy 的 APK 复刻（拦截决策）
  assets/blocklist.json          # 拦截清单（生成物，测试强制同步）
  res/                           # 主题 / 自适应图标（无 PNG）
```
