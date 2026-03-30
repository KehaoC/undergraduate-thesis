# Taste Skill Experiments

这个目录承载论文 Day 1 的实验准备工作：

- 从 GitHub 发现 Anthropic/Claude 风格的 skill 文件
- 解析 `name / description / frontmatter / content / github_url`
- 批量补充 GitHub star，清洗出三档实验数据集
- 搭建后续 SkillRank 实验所需的 Python 环境

当前数据集语义已经调整为“skill 是一个文件树”：

- `frontmatter`：仅保存 `SKILL.md` YAML 头
- `content`：保存 skill 根目录下的文件树结构
- `content.files[].content`：保存各文本文件内容
- `content_text`：把整个文件树中的文本内容按路径拼平，便于后续向量化/检索实验

其中 `SKILL.md` 的 `content` 会去掉 YAML 头，只保留正文；`references/`、`scripts/`、`assets/` 等同级文件/目录会按相对路径保留下来。二进制文件只保留路径和元信息，不内联字节内容。

## 环境准备

优先使用已登录的 `gh` 凭证；如果没有，也可以显式设置 `GITHUB_TOKEN`。

```bash
cd /Users/kehao/CodeSpace/undergraduate-thesis/code
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

## 构建数据集

```bash
cd /Users/kehao/CodeSpace/undergraduate-thesis/code
source .venv/bin/activate
python scripts/build_skill_datasets.py \
  --output-dir data \
  --raw-target 15000 \
  --final-counts 100,1000,10000
```

脚本会缓存中间结果到 `data/cache/`，中途中断后可直接重跑继续。
