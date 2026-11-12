# 双盲评审期的匿名托管策略（2026-11-12）

**问题**：NeurIPS D&E 要求"投稿时数据/代码已托管、可访问、有文档"，但默认双盲；HuggingFace/GitHub 链接会暴露账号身份。

**方案（推荐）**：
1. **评审期**：用 **anonymous.4open.science**（或等价匿名快照服务）上传本包 `hf_dataset/` 的压缩包；
   链接形如 `https://anonymous.4open.science/r/<random-id>`，不含作者信息；
   论文中只写匿名链接 + 数据卡/复现说明。
2. **录用后**：把同一内容上传到正式 HuggingFace 数据集（本包 `README.md` 即 dataset card），
   替换论文中的匿名链接，并补 DOI（可选）。
3. **禁止**：评审期用带真实用户名的 HF/GitHub 链接；预印本标 "Under review at NeurIPS"。

**要交给审稿人的最小内容**：
- `hf_dataset/` 全部文件（含 code package 可另附 `cec-ad/` 目录）；
- `croissant.json`（评审期可把 contentUrl 暂时指向匿名快照内的相对路径）；
- 复现命令见 code package `README.md` / `code/repro/run_all.py`。

**注意**：HuggingFace 的 `huggingface-cli upload` 需要账号，评审期不用；匿名快照只需上传 zip。
