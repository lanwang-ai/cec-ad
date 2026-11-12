# Hugging Face 上传步骤（2026-11-12）

## 0. 准备
```bash
python -m pip install -U "huggingface_hub[cli]"
huggingface-cli login        # 个人 token（write 权限）
```

## 1. 创建数据集仓库
```bash
huggingface-cli repo create cec-ad --type dataset
# 或网页: https://huggingface.co/new-dataset
```

## 2. 上传
```bash
cd _public_bundle/hf_dataset
huggingface-cli upload <HF_USER>/cec-ad . --repo-type dataset
```

## 3. 回填 Croissant / 数据卡
- 上传后 URL 形如 `https://huggingface.co/datasets/<HF_USER>/cec-ad/resolve/main/<path>`；
- 把 `croissant.json` 中 `<HF_USER>` 替换为实际用户名、`<COMMIT>` 替换为 commit hash（或 `main`）；
- `sha256_manifest.json` 已含本地文件哈希，上传后可对照 HF 文件页的 hash 或重新计算；
- 论文/README 中引用数据集链接。

## 4. 评审期匿名策略
- 见 `docs/review_anonymity.md`：评审期用 **anonymous.4open.science** 快照（代码/数据打包），
  录用后切换到正式 HuggingFace 链接；两边内容一致（本包即内容源）。

## 5. 校验
```bash
python -X utf8 实验/P2_20261108/prepare_hf_kit.py   # 重新生成 + 校验 sha256
```
