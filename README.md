# Desktop Icon Toggle

一键隐藏/显示 Windows 桌面图标的轻量工具，支持系统托盘常驻。

## 功能

- 隐藏 / 显示 Windows 桌面图标
- 关闭窗口 → 最小化到系统托盘（不退出）
- 系统托盘右键菜单：显示图标、隐藏图标、退出
- 双击托盘图标快速切换
- 退出时自动恢复桌面图标

## 使用

运行 `desktop_icon_toggle.exe`，或从源码启动：

```bash
pip install pywin32
python desktop_icon_toggle.py
```

## 打包

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name desktop_icon_toggle desktop_icon_toggle.py
```
