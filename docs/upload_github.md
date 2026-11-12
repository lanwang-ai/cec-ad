# GitHub 上传与匿名快照（2026-11-12）

## 为什么 GitHub 可以
NeurIPS D&E 要求“投稿时数据/代码可访问、有文档、有 Croissant”。GitHub 仓库满足：
- 本包 **约 9 MB**（最大文件 7.9 MB < GitHub 100 MB/文件限制，无需 Git LFS）；
- 仓库自带 `README.md` 数据卡、`LICENSE`、`requirements.txt`、`code/repro/run_all.py`；
- 根目录 `croissant.json` 提供机器可读元数据（评审可校验）。

## 双盲要求（重要）
**评审期不要用个人 GitHub 链接**（会暴露身份）。两种合规做法：
1. **anonymous.4open.science（推荐）**：把本仓库 push 到 GitHub（个人或临时匿名账号均可，最好匿名账号），到 https://anonymous.4open.science 提交仓库 URL，生成匿名链接 `https://anonymous.4open.science/r/<id>`，论文只写该链接；
2. 临时匿名 GitHub 账号直接建仓库（用户名/邮箱不含真实信息），评审期直接用其链接。

录用后：把同一内容迁移到正式 HF 数据集或个人 GitHub，替换论文链接即可。

## 上传步骤（你自己操作，或给我 token 我代传）
```bash
# 1) 建空仓库（GitHub 网页 Create repository，不要初始化 README）
# 2) 本地初始化并推送
cd "E:\职业规划\04_项目与竞赛\论文转化投稿\_public_bundle\github_repo"
git init -b main
git add -A
git commit -m "CEC-AD benchmark release v1"
git remote add origin https://github.com/<USER>/cec-ad.git
git push -u origin main
```

## 回填 Croissant
设置环境变量 `GITHUB_USER=<你的用户名>` 后重新跑本脚本，`contentUrl` 会自动替换为
`https://raw.githubusercontent.com/<USER>/cec-ad/main/...`；或手动全局替换 `croissant.json` 中的 `<GITHUB_USER>`。

## 匿名快照
```bash
# GitHub 仓库 push 后
# 打开 https://anonymous.4open.science → 输入 https://github.com/<USER>/cec-ad → 生成匿名链接
```
