# 屏时起身：软件时长统计与起立活动提醒 / Screen-Time Rise: App Usage Tracking and Stand-Up Activity Reminders

屏时起身是一个轻量的 Windows 小工具：它会统计当前前台软件的使用时长，并在连续使用屏幕一段时间后提醒你起身活动或调整站姿。

PingShiQiShen is a lightweight Windows utility that tracks foreground app usage time and reminds you to stand up, move, or correct your standing posture during long screen sessions.

## 功能

- 记录当前前台软件的使用时长。
- 显示本轮实时统计和历史总计。
- 坐下办公时，连续使用屏幕 30 分钟提醒起身活动。
- 站立办公时，每 30 分钟提醒收腹、站直、不要把重心偏在一边。
- 深色调和金色点缀界面，提醒弹窗为独立小卡片。
- 支持关闭提醒，用于沉浸式工作。
- 支持开机自启动。
- 数据只保存在本机。

## 下载使用

普通用户可以在 GitHub Releases 下载 Windows zip 附件（显示为 `屏时起身-普通用户版.zip`）。

1. 解压 zip。
2. 双击 `屏时起身.exe` 直接运行。
3. 想开机自动启动时，双击 `安装开机启动.cmd`。
4. 不想开机启动时，双击 `卸载开机启动.cmd`。

Windows 可能提示“未知发布者”。这是因为当前版本没有商业代码签名，确认来源可信后，可选择“更多信息”再选择“仍要运行”。

## 从源码运行

```powershell
python src\pingshi_qishen.pyw
```

## 构建 exe

需要 Windows 和 Python。构建脚本会在项目内创建 `.venv` 并安装 PyInstaller。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1
```

构建完成后，产物会出现在 `dist\屏时起身-普通用户版.zip`。

## 数据与隐私

记录文件默认保存在：

```text
%LOCALAPPDATA%\PingShiQiShen\sessions.json
```

数据不会上传。程序会记录前台软件名称、可执行文件路径和窗口标题，所以请把它当作个人使用数据。

## English

### Features

- Tracks foreground app usage time on Windows.
- Shows live session statistics and historical totals.
- Reminds you to stand up after 30 minutes of active seated screen use.
- In standing mode, reminds you every 30 minutes to keep posture balanced.
- Dark UI with gold accents and compact reminder cards.
- Lets you disable reminders for focused work.
- Supports Windows startup autostart.
- Stores data locally only.

### Download

Download `屏时起身-普通用户版.zip` from GitHub Releases, unzip it, and run `屏时起身.exe`.

### Build

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1
```

## License

MIT
