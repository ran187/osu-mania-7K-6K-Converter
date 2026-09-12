# osu!mania 7K → 6K 谱面转换工具

将 osu!mania 7K 谱面转换为 6K 谱面。

## 功能

### b.py

- 一次性转换所有已导入的谱面

### a.py

- 扫描 Downloads 文件夹中的 `.osz` 文件（未导入）。
- 转换 `.osz` 中的 7K 谱面为 6K

## 用法

### b.py

1. **修改路径**：编辑 `b.py` 中的 `get_songs_dir()` 函数，把返回值改成你自己的 osu! Songs 目录
   （osu! 内 Options → "Open osu! folder" → 进入 `Songs` 文件夹即可得到）。
2. 运行：
   ```bash
   python b.py
   ```
3. 程序会列出 Songs 目录下所有谱面集，按 Enter 开始批量转换。

### a.py

1. **修改路径**：编辑 `a.py` 文件顶部的 `DOWNLOADS` 常量，改成你自己的下载目录。
2. 将需要转换的 `.osz` 谱面包放入该目录，运行：
   ```bash
   python a.py
   ```
3. 程序列出待处理的谱面包，输入 `Y` 确认后自动完成解压、转换、重新打包。

## 作者

- **Claude**（Anthropic）
