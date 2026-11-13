# ogg2mp3

将 OGG / Ogg Vorbis / Ogg Opus 转为 MP3，并尽量完整保留元数据与封面图。

依赖：`ffmpeg`、`ffprobe`（无需 Python 第三方库）。

## 用法

```bash
# 转换当前目录（不递归）
python3 ogg2mp3.py .

# 转换单个文件，指定输出目录
python3 ogg2mp3.py song.ogg -o converted/

# 用外部图片作为封面（适用于源文件封面是放大图的情况）
python3 ogg2mp3.py song.ogg --cover cover-800.jpg --overwrite

# 递归、覆盖已有文件、查看将要做什么
python3 ogg2mp3.py music/ -r --overwrite
python3 ogg2mp3.py music/ -r --dry-run
```

默认码率 320k，可用 `-b 192k` 等调整。

## 元数据处理

- 标准标签映射到 ID3v2.3：`TITLE`→`TIT2`、`ARTIST`→`TPE1`、`ALBUM`→`TALB`、
  `ALBUMARTIST`→`TPE2`、`TRACKNUMBER`→`TRCK`、`DATE`→`TYER`、`COMMENT`→`COMM`、
  `LYRICS`→`USLT` 等。
- 未映射的标签写入 `TXXX` 帧，不会静默丢弃。
- 封面优先取 attached picture 流，其次解析 Vorbis comment 里的
  `METADATA_BLOCK_PICTURE` / `COVERART`，以原始字节写入 ID3 `APIC` 帧，不重新压缩。
- 文本使用 UTF-16 编码，中文等非 ASCII 标签可正常显示。
- 转码时关闭 ffmpeg 自身的 ID3 写入，由脚本统一生成标签，避免出现重复 ID3 块。

### macOS 自定义文件图标（QQ 音乐 / 网易云音乐）

一些下载器会把封面写进文件的 **macOS 自定义图标**（`com.apple.ResourceFork`
中的 `icns` 资源），而不是音频元数据。ffprobe 看不到这种封面，但 Finder 会显示。
本工具会：

1. 解析 resource fork 里的 `icns`，取分辨率最高的 PNG（如 1024×1024）；
2. 原样嵌入 MP3 的 `APIC` 帧；
3. 把 `com.apple.FinderInfo`、`com.apple.ResourceFork` 复制到输出 MP3，
   让 Finder 图标也保持一致。

如果源文件本身既没有内嵌封面也没有自定义图标，输出 MP3 也不会有封面。

### 封面清晰度与 `--cover`

工具会自动选择能拿到的最大封面，但“最大尺寸”不一定等于“最清晰”：一些播放器会把小图
放大后写进 icns（例如 1024×1024 实际是 512×512 放大的）。这种情况下可以用
`--cover` 指定一张真正的原图：

```bash
python3 ogg2mp3.py song.ogg --cover cover-800.jpg --overwrite
```

`--cover` 会按图片原始字节写入 `APIC`，不重新压缩；对多个输入文件会统一使用这张图。
