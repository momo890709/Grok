# 视频转 GIF

本地小网页，两种用法：

- **视频路径**：贴一个或多个视频路径（一行一个），直接转 GIF，GIF 存在原视频旁边。
- **PPT 提取**：贴 `.pptx` 路径，把里面嵌入（以及仍存在的外链）视频全部列出来，标好在第几页，勾选后批量转 GIF，存到 PPT 旁边的 `xxx_gifs/`，文件名带页码。

可调帧率（默认 6 fps，最低 2，抽帧越狠越省体积）、宽度、颜色数、抖动方式、速度和截取区间。

## 运行

依赖只有 Python 3 和 ffmpeg（Mac：`brew install ffmpeg`）。

```bash
python3 server.py          # 自动打开 http://127.0.0.1:8765
python3 server.py --port 9000 --no-browser
```

Mac 上也可以直接双击 `启动.command`。第一次双击如果被拦，右键 → 打开。

服务只监听 `127.0.0.1`，PPT 里的视频会解压到系统临时目录的 `gif-tool-cache/`。老格式 `.ppt` 需要先另存为 `.pptx`。
